"""The ci-observe MCP server (ci_observe_mcp.py): typed questions over the
exporter API, answered with rows, the exact API URL, a Grafana URL and the
about row -- and a plain reason when the question is not answerable. Plus
the GitHub-OAuth gate that admits anyone with a Grafana user and nobody
else."""

from __future__ import annotations

import importlib.util
import json
import math
import re
import socket
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mcp = load("ci_observe_mcp")


class FakeWeb:
    """Routes requests.get by URL, the way the exporter tests fake GitHub."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.calls: list[str] = []

    def add(self, url, payload, status=200):
        self.routes[url] = (status, payload)

    def get(self, url, **kwargs):
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            return FakeStatus(404, {"message": "not routed"})

        class Answer:
            def __init__(self, status, payload):
                self.status_code = status
                self.ok = 200 <= status < 300
                self._payload = payload

            def json(self):
                return self._payload

        return Answer(*route)


class FakeStatus:
    def __init__(self, status, payload):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload

    def json(self):
        return self._payload


def grafana_user(login, role="Viewer", disabled=False, labels=("GitHub",)):
    """One /api/org/users entry, the fields Grafana 13 answers with."""
    return {"login": login, "role": role, "isDisabled": disabled, "authLabels": list(labels)}


@pytest.fixture
def web(monkeypatch):
    fake = FakeWeb()
    monkeypatch.setattr(mcp.requests, "get", fake.get)
    return fake


@pytest.fixture
def config():
    return mcp.Config(
        bind="127.0.0.1", port=0, exporter_url="http://exporter", grafana_url="https://grafana",
        grafana_api_url="https://grafana", external_url="https://outside",
        github_client_id="cid", github_client_secret="secret",
        grafana_service_token="token", session_secret="session-secret", github_api_url="https://gh",
        grafana_verify_tls=False,
    )


def rpc(config, payload, now=1000000.0):
    return mcp.handle_rpc(config, payload, now)


def tool_call(config, name, arguments):
    return mcp.call_tool(config, name, arguments)


# -- configuration ------------------------------------------------------------------


def test_a_missing_configuration_lists_every_absent_variable(monkeypatch):
    for name in mcp.Config.REQUIRED:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as stopped:
        mcp.Config.from_env()
    message = str(stopped.value)
    for name in mcp.Config.REQUIRED:
        assert name in message
    assert "refuses to start" in message


def test_a_complete_configuration_builds(monkeypatch):
    values = {
        "MCP_GITHUB_CLIENT_ID": "cid", "MCP_GITHUB_CLIENT_SECRET": "secret",
        "MCP_GRAFANA_URL": "https://grafana", "MCP_GRAFANA_SERVICE_TOKEN": "token",
        "MCP_SESSION_SECRET": "session-secret", "MCP_EXTERNAL_URL": "https://outside/mcp",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    config = mcp.Config.from_env()
    assert config.port == 9105
    assert config.exporter_url == "http://127.0.0.1:9103"


# -- the signed tokens ---------------------------------------------------------------


def test_a_token_round_trips_and_expires():
    token = mcp.mint_token("secret", "dev", now=1000.0)
    assert mcp.read_token("secret", token, now=1000.0 + 1) == "dev"
    assert mcp.read_token("secret", token, now=1000.0 + mcp.TOKEN_SECONDS + 1) is None


def test_a_forged_token_is_refused():
    token = mcp.mint_token("secret", "dev", now=1000.0)
    assert mcp.read_token("other-secret", token, now=1000.0) is None
    assert mcp.read_token("secret", token[:-1] + ("0" if token[-1] != "0" else "1"), now=1000.0) is None
    assert mcp.read_token("secret", "not-a-token", now=1000.0) is None


# -- the JSON-RPC surface ------------------------------------------------------------


def test_initialize_negotiates_the_protocol_and_names_the_server(config):
    status, answer = rpc(config, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-03-26"}})
    assert status == HTTPStatus.OK
    assert answer["result"]["protocolVersion"] == "2025-03-26"
    assert answer["result"]["serverInfo"]["name"] == "ci-observe"


def test_initialize_answers_an_unknown_version_with_ours(config):
    _, answer = rpc(config, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "1999-01-01"}})
    assert answer["result"]["protocolVersion"] == "2025-06-18"


def test_tools_list_names_every_tool_with_its_required_parameters(config):
    _, answer = rpc(config, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in answer["result"]["tools"]}
    assert set(tools) == {
        "suite_health", "failing_tests", "flaky_tests", "failure_causes", "failure_signatures",
        "time_to_result", "slowest_tests", "time_phases", "waits", "test_history", "run_failed_tests",
        "list_runs", "pull_request_stats", "pull_requests", "pull_request_detail", "incidents",
    }
    # Every population dimension is required with no default: the agent
    # must ask which suite, trigger, repository and window it means.
    assert tools["suite_health"]["inputSchema"]["required"] == ["suite", "trigger", "repo", "days"]
    assert tools["suite_health"]["inputSchema"]["properties"]["suite"]["enum"] == list(mcp.SUITES)
    assert tools["suite_health"]["inputSchema"]["properties"]["trigger"]["enum"] == list(mcp.TRIGGERS)
    # The incidents tool asks for a window only: an incident is not per
    # suite, trigger or repository.
    assert tools["incidents"]["inputSchema"]["required"] == ["days"]


def test_the_suite_vocabulary_covers_the_integration_suites(config):
    """The four Tests-workflow integration suites are askable, and so are
    the push trigger they run under and the flake-detection campaigns
    (which only an explicit trigger picks)."""
    assert mcp.SUITES == ("vmaas", "caas", "bmaas", "it-fulfillment", "it-operator", "it-bmf", "it-caas")
    assert "push" in mcp.TRIGGERS
    assert "flake" in mcp.TRIGGERS
    _, answer = rpc(config, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in answer["result"]["tools"]}
    for name in ("suite_health", "failing_tests", "test_history", "list_runs"):
        assert tools[name]["inputSchema"]["properties"]["suite"]["enum"] == list(mcp.SUITES), name


def test_a_notification_answers_nothing(config):
    status, answer = rpc(config, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == HTTPStatus.ACCEPTED and answer is None


def test_an_unknown_method_is_a_json_rpc_error(config):
    with pytest.raises(mcp.JsonRpcError) as error:
        rpc(config, {"jsonrpc": "2.0", "id": 3, "method": "no/such"})
    assert error.value.code == -32601


# -- the tools -----------------------------------------------------------------------


def test_a_health_question_returns_rows_urls_and_the_about_row(config, web):
    web.add("http://exporter/api/ci-observe/health?days=7&suite=vmaas&trigger=pr&repo=osac", {
        "suites": [{"suite": "vmaas", "runs": 10, "pass_rate": 0.9}],
        "about": [{"population": "VMaaS suite runs"}],
        "window": [{"days": 7}],
        "notes": [],
    })
    answer = tool_call(config, "suite_health",
                       {"suite": "vmaas", "trigger": "pr", "repo": "osac", "days": 7})
    assert answer["rows"] == [{"suite": "vmaas", "runs": 10, "pass_rate": 0.9}]
    assert answer["api_url"] == ("http://exporter/api/ci-observe/health"
                                 "?days=7&suite=vmaas&trigger=pr&repo=osac")
    assert answer["grafana_url"] == ("https://grafana/d/ci-observe-home/ci-observe-e28094-overview"
                                     "?var-days=7&var-suite=vmaas&var-trigger=pr&var-repo=osac")
    assert answer["about"] == {"population": "VMaaS suite runs"}
    assert answer["not_answerable"] == ""


def days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def test_an_end_reaches_the_api_and_the_question(config, web):
    end = days_ago(3)
    web.add("http://exporter/api/ci-observe/failures?days=7&suite=vmaas&trigger=pr&repo=osac"
            f"&end={end}T00%3A00%3A00Z", {"flaky": [{"test": "t"}], "about": [{}], "window": [{}], "notes": []})
    answer = tool_call(config, "flaky_tests",
                       {"suite": "vmaas", "trigger": "pr", "repo": "osac", "days": 7, "end": end})
    assert answer["api_url"].endswith(f"end={end}T00%3A00%3A00Z")
    assert f"ending {end}T00:00:00Z" in answer["question"]


@pytest.mark.parametrize(("end", "reason"), [
    ("2026-11-01T12:00:00+02:00", "must be a UTC date"),  # the API would answer it for a window ending now
    ("2026-11-01 12:00", "must be a UTC date"),
    ("not a date", "must be a UTC date"),
    (7, "must be a UTC date"),
])
def test_an_end_the_api_cannot_read_is_refused_before_any_call(config, web, end, reason):
    with pytest.raises(mcp.ToolError, match=reason):
        tool_call(config, "suite_health", {"suite": "vmaas", "trigger": "pr", "repo": "osac", "days": 7,
                                           "end": end})
    assert web.calls == []


def test_a_future_end_is_refused(config, web):
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    with pytest.raises(mcp.ToolError, match="in the future"):
        tool_call(config, "suite_health", {"suite": "vmaas", "trigger": "pr", "repo": "osac", "days": 7,
                                           "end": tomorrow})
    assert web.calls == []


def test_a_window_ending_in_the_past_must_lie_inside_the_retention(config, web):
    # 30 days ending 70 days ago reach back 100: past the 90-day archive.
    with pytest.raises(mcp.ToolError, match="starts before the 90 days"):
        tool_call(config, "suite_health", {"suite": "vmaas", "trigger": "pr", "repo": "osac", "days": 30,
                                           "end": days_ago(70)})
    # The job history keeps 30 days: 7 days ending 25 days ago reach 32.
    with pytest.raises(mcp.ToolError, match="starts before the 30 days the job history keeps"):
        tool_call(config, "list_runs", {"suite": "vmaas", "result": "failure", "repo": "osac", "days": 7,
                                        "end": days_ago(25)})
    with pytest.raises(mcp.ToolError, match="starts before the 90 days"):
        tool_call(config, "pull_requests", {"repo": "osac", "days": 30, "state": "merged", "end": days_ago(80)})
    assert web.calls == []


def test_a_failed_tests_502_is_the_reason_not_an_empty_answer(config, web):
    web.add("http://exporter/api/ci-observe/failed-tests?capture_id=vmaas-main-42-a1",
            {"error": "no osac-logs-full-install-42 artifact in osac-project/osac-ci-artifacts,"
                      " where scheduled runs keep it"}, status=502)
    answer = tool_call(config, "run_failed_tests", {"capture_id": "vmaas-main-42-a1"})
    assert answer["rows"] == []
    assert "osac-ci-artifacts" in answer["not_answerable"]
    assert "42" in answer["not_answerable"]


def test_a_run_whose_junit_lists_no_failure_says_so(config, web):
    web.add("http://exporter/api/ci-observe/failed-tests?capture_id=vmaas-main-42-a1",
            {"failed_tests": []})
    answer = tool_call(config, "run_failed_tests", {"capture_id": "vmaas-main-42-a1"})
    assert answer["not_answerable"] == ""
    assert answer["notes"] == ["the junit.xml of vmaas-main-42-a1 lists no failed test"]


def test_an_unrecorded_run_is_not_answerable(config, web):
    web.add("http://exporter/api/ci-observe/runs?days=7&repo=osac&suite=vmaas&result=failure&limit=20",
            {"runs": [], "truncated": False})
    answer = tool_call(config, "list_runs",
                       {"suite": "vmaas", "result": "failure", "repo": "osac", "days": 7})
    assert answer["not_answerable"] == "no runs match in this window"


def test_the_exporter_being_down_is_not_answerable(config, web):
    web.add("http://exporter/api/ci-observe/health?days=7&suite=vmaas&trigger=pr&repo=osac",
            {"message": "down"}, status=500)
    answer = tool_call(config, "suite_health",
                       {"suite": "vmaas", "trigger": "pr", "repo": "osac", "days": 7})
    assert "HTTP 500" in answer["not_answerable"]


def test_rows_are_capped_at_twenty(config, web):
    web.add("http://exporter/api/ci-observe/failures?days=7&suite=vmaas&trigger=pr&repo=osac",
            {"tests": [{"test": f"t{i}"} for i in range(50)], "about": [{}], "window": [{}],
             "notes": []})
    answer = tool_call(config, "failing_tests",
                       {"suite": "vmaas", "trigger": "pr", "repo": "osac", "days": 7})
    assert len(answer["rows"]) == mcp.MAX_ROWS == 20
    # A cut list says it is one: how many there were and that there are more.
    assert answer["rows_total"] == 50
    assert any("showing 20 of 50" in note for note in answer["notes"])


def test_a_list_that_fits_has_no_cut_note(config, web):
    web.add("http://exporter/api/ci-observe/failures?days=7&suite=vmaas&trigger=pr&repo=osac",
            {"tests": [{"test": "t"}], "about": [{}], "window": [{}], "notes": []})
    answer = tool_call(config, "failing_tests", {"suite": "vmaas", "trigger": "pr", "repo": "osac", "days": 7})
    assert answer["rows_total"] == 1 and not any("showing" in note for note in answer["notes"])


def test_a_capped_runs_page_names_its_total_and_the_next_page(config, web):
    """The exporter pages by run id (keyset): the next page is the same
    window, older than the last run shown, so a page neither repeats nor
    skips a run and a 30-day window pages to its end. Moving end back, the
    advice before 2026-11-22, moved the window out of the job history."""
    web.add("http://exporter/api/ci-observe/runs?days=30&repo=osac&suite=vmaas&result=failure&limit=20",
            {"runs": [{"id": str(900 - i)} for i in range(20)], "truncated": True, "total": 57,
             "next_before": "881"})
    web.add("http://exporter/api/ci-observe/runs?days=30&repo=osac&suite=vmaas&result=failure&limit=20&before=881",
            {"runs": [{"id": str(880 - i)} for i in range(20)], "truncated": True, "total": 57,
             "next_before": "861"})
    first = tool_call(config, "list_runs", {"suite": "vmaas", "result": "failure", "repo": "osac", "days": 30})
    assert first["rows_total"] == 57
    assert first["next_page"] == {"before": "881"}
    assert any("20 of the 57 runs" in note and "before=881" in note for note in first["notes"])
    second = tool_call(config, "list_runs", {"suite": "vmaas", "result": "failure", "repo": "osac", "days": 30,
                                             **first["next_page"]})
    assert {row["id"] for row in first["rows"]}.isdisjoint(row["id"] for row in second["rows"])
    assert second["next_page"] == {"before": "861"}


def test_a_runs_page_can_hold_up_to_a_hundred_and_the_cursor_is_checked(config, web):
    web.add("http://exporter/api/ci-observe/runs?days=7&repo=osac&suite=vmaas&result=failure&limit=100",
            {"runs": [{"id": str(i)} for i in range(57)], "truncated": False, "total": 57, "next_before": None})
    answer = tool_call(config, "list_runs", {"suite": "vmaas", "result": "failure", "repo": "osac", "days": 7,
                                             "limit": 100})
    assert len(answer["rows"]) == 57 and "next_page" not in answer
    for bad in ("41310072671; DROP", "-1", 881, "1" * 21):
        with pytest.raises(mcp.ToolError, match="before"):
            mcp.TOOL_BY_NAME["list_runs"].handler(config, {"suite": "vmaas", "result": "failure", "repo": "osac",
                                                           "days": 7, "before": bad})
    with pytest.raises(mcp.ToolError):
        mcp.TOOL_BY_NAME["list_runs"].handler(config, {"suite": "vmaas", "result": "failure", "repo": "osac",
                                                       "days": 7, "limit": 101})


def pr_row(number, merged_days_ago=None, opened_days_ago=40, closed_days_ago=None, state="merged"):
    def moment(days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {"number": number, "repo": "osac", "title": f"PR {number}", "state": state,
            "opened_at": moment(opened_days_ago),
            "merged_at": moment(merged_days_ago) if merged_days_ago is not None else None,
            "closed_at": moment(closed_days_ago) if closed_days_ago is not None else None}


def test_the_pull_requests_of_a_past_window_are_its_own(config, web, monkeypatch):
    # Window: the 7 days ending 10 days ago. #1 merged after it, #2 and #3 inside, #4 before it,
    # #5 was closed inside it (the index windows a closed one by its close).
    rows = [pr_row(1, merged_days_ago=5), pr_row(2, merged_days_ago=11), pr_row(3, merged_days_ago=16),
            pr_row(4, merged_days_ago=18), pr_row(5, closed_days_ago=12, state="closed")]
    # One fixed clock for the rows, the expected URL and the tool: UTC
    # midnight passing between them would round the fetch to another day.
    clock = datetime.now(timezone.utc)
    monkeypatch.setattr(mcp, "datetime", type("Fixed", (type(clock),), {"now": staticmethod(lambda tz=None: clock)}))
    end = (clock - timedelta(days=10)).strftime("%Y-%m-%d")
    start = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc) - timedelta(days=7)
    fetch = math.ceil((clock - start).total_seconds() / 86400)
    web.add(f"http://exporter/api/ci-observe/prs?days={fetch}&repo=osac", {"prs": rows, "about": [{}], "notes": []})
    answer = tool_call(config, "pull_requests", {"repo": "osac", "days": 7, "state": "all", "end": end})
    assert [row["number"] for row in answer["rows"]] == [2, 5, 3]
    assert answer["rows_total"] == 3
    merged = tool_call(config, "pull_requests", {"repo": "osac", "days": 7, "state": "merged", "end": end})
    assert [row["number"] for row in merged["rows"]] == [2, 3]


def test_more_than_twenty_pull_requests_page_through_the_same_window(config, web):
    """The cursor is (moment, number): ties on a moment neither repeat nor
    skip, and every page is the window asked for. Before 2026-11-22 the
    advice was end=<oldest shown>, which repeated that pull request, moved
    the window's start back by the same days, and on a tie of more than
    twenty answered the same page forever."""
    # Older than all thirty, so the 25 that share one moment span two pages.
    tied = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [pr_row(number, merged_days_ago=1 + number / 10) for number in range(1, 31)]
    rows += [{**pr_row(number), "merged_at": tied} for number in range(100, 125)]
    web.add("http://exporter/api/ci-observe/prs?days=7&repo=osac", {"prs": rows, "about": [{}], "notes": []})
    seen = []
    arguments = {"repo": "osac", "days": 7, "state": "merged"}
    for _ in range(5):
        answer = tool_call(config, "pull_requests", arguments)
        assert answer["rows_total"] == 55
        seen += [row["number"] for row in answer["rows"]]
        if "next_page" not in answer:
            break
        assert any(f"before={answer['next_page']['before']}" in note for note in answer["notes"])
        arguments = {**arguments, **answer["next_page"]}
    assert seen[:20] == list(range(1, 21))  # the latest first
    assert sorted(seen) == sorted(row["number"] for row in rows), "every pull request once, no repeats"
    with pytest.raises(mcp.ToolError, match="before"):
        mcp.TOOL_BY_NAME["pull_requests"].handler(config, {**arguments, "before": "yesterday"})


def test_a_failing_tool_is_an_error_answer_not_a_reset_connection(config, monkeypatch):
    def broken(config, arguments):
        raise KeyError("boom")

    monkeypatch.setitem(mcp.TOOL_BY_NAME, "suite_health",
                        mcp.Tool("suite_health", "", {}, broken))
    status, answer = rpc(config, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                  "params": {"name": "suite_health", "arguments": {}}})
    assert answer["result"]["isError"] is True
    assert "failed answering" in answer["result"]["content"][0]["text"]


def test_initialize_survives_params_that_are_not_an_object(config):
    status, answer = rpc(config, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": []})
    assert answer["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]


def test_every_population_dimension_is_required(config):
    for missing in ("suite", "trigger", "repo", "days"):
        arguments = {"suite": "vmaas", "trigger": "pr", "repo": "osac", "days": 7}
        arguments.pop(missing)
        with pytest.raises(mcp.ToolError, match=missing):
            tool_call(config, "suite_health", arguments)


def test_a_window_beyond_the_retention_is_refused_before_any_call(config, web):
    with pytest.raises(mcp.ToolError, match="between 1 and 90"):
        tool_call(config, "suite_health", {"suite": "vmaas", "trigger": "pr", "repo": "osac",
                                           "days": 91})
    with pytest.raises(mcp.ToolError, match="between 1 and 30"):
        tool_call(config, "list_runs", {"suite": "vmaas", "result": "failure", "repo": "osac",
                                        "days": 31})
    assert web.calls == []


def test_a_bad_capture_id_is_refused_before_any_call(config, web):
    with pytest.raises(mcp.ToolError, match="capture_id must look like"):
        tool_call(config, "run_failed_tests", {"capture_id": "not-a-capture"})
    assert web.calls == []


@pytest.mark.parametrize("capture_id", [
    "it-fulfillment-pr2530-45123456789-a1",
    "it-operator-main-45123456790-a2",
    "it-bmf-pr7-45123456791-a1",
    "it-caas-main-45123456792-a1",
])
def test_an_integration_capture_id_names_its_suite_artifact(config, capture_id):
    """The run_failed_tests capture-id pattern accepts the four integration
    suites: their bundle is named after the suite the id carries."""
    assert re.match(mcp.CAPTURE_ID_RULE, capture_id)


def test_a_suite_nobody_knows_is_still_refused(config):
    assert not re.match(mcp.CAPTURE_ID_RULE, "it-nosuchsuite-pr1-45123456789-a1")
    assert not re.match(mcp.CAPTURE_ID_RULE, "netris-pr1-45123456789-a1")


def test_an_unknown_tool_is_a_tool_error(config):
    with pytest.raises(mcp.ToolError, match="unknown tool"):
        tool_call(config, "no_such_tool", {})


def test_a_tool_error_is_an_error_result_not_a_crash(config, web):
    status, answer = rpc(config, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                  "params": {"name": "suite_health", "arguments": {}}})
    assert status == HTTPStatus.OK
    assert answer["result"]["isError"] is True
    assert "suite is required" in answer["result"]["content"][0]["text"]


def test_a_test_history_question_passes_the_test(config, web):
    web.add("http://exporter/api/ci-observe/test?days=30&suite=vmaas&trigger=pr&repo=osac"
            "&test=tests.e2e.vmaas.sanity.test_vm%3A%3ATestVm%3A%3Atest_create",
            {"summary": {"runs": 5}, "runs": [{"run_id": 1}], "about": [{}], "window": [{}],
             "notes": []})
    answer = tool_call(config, "test_history",
                       {"test": "tests.e2e.vmaas.sanity.test_vm::TestVm::test_create",
                        "suite": "vmaas", "trigger": "pr", "repo": "osac", "days": 30})
    assert answer["rows"][0] == {"runs": 5}
    assert "var-test=" in answer["grafana_url"]


def test_pull_request_rows_come_from_the_kpis_and_the_funnel(config, web):
    # The document's kpis is a list of one row, as ci_observe_prs serves it.
    web.add("http://exporter/api/ci-observe/prs?days=30&repo=osac",
            {"kpis": [{"merged": 10}], "funnel": [{"stage": "review"}], "about": [{}], "notes": []})
    answer = tool_call(config, "pull_request_stats", {"repo": "osac", "days": 30})
    assert answer["rows"] == [{"merged": 10}, {"stage": "review"}]


def test_the_pull_request_numbers_include_the_real_document_kpis(config, web):
    """The served document, not a hand-made one: its kpis row must reach
    the answer (a dict-only check dropped it)."""
    prs = load("ci_observe_prs")
    document = {"kpis": prs._kpis([], {}), "funnel": prs._funnel([]), "about": [{}], "notes": []}
    web.add("http://exporter/api/ci-observe/prs?days=30&repo=osac", document)
    answer = tool_call(config, "pull_request_stats", {"repo": "osac", "days": 30})
    assert "lead_median_s" in answer["rows"][0]


# -- the incidents tool ------------------------------------------------------------------


def seeded_incident():
    """One incident record as the exporter's incidents document serves it."""
    return {
        "id": "INC-2026-11-install", "title": "Install failures from defective snapshots",
        "status": "identified", "diagnosis_confidence": "confirmed", "supersedes": "",
        "window_from": "2026-11-05T00:00:00Z", "window_to": "",
        "areas": ["install"], "suites": ["vmaas", "caas", "bmaas"], "repos": ["osac", "osac-test-infra"],
        "summary": "Install failures doubled weekly from Nov 5.",
        "contributing_factors": ["The flavor freshness check failed silently."],
        "evidence": [{"label": "the post-mortem", "url": "https://example.com/pm"}],
        "follow_ups": [{"text": "rebuild the snapshot", "owner": "", "due": "", "ticket": "",
                        "state": "open", "declined_reason": ""}],
    }


def incidents_document(rows, unavailable=""):
    document = {
        "incidents": rows,
        "about": [{"population": "recorded CI incidents whose window overlaps"}],
        "window": [{"days": 7}],
        "notes": [],
    }
    if unavailable:
        document["unavailable"] = unavailable
    return document


def test_an_incidents_question_returns_rows_urls_and_the_about_row(config, web):
    web.add("http://exporter/api/ci-observe/incidents?days=7",
            incidents_document([seeded_incident()]))
    answer = tool_call(config, "incidents", {"days": 7})
    assert answer["rows"] == [seeded_incident()]
    assert answer["rows_total"] == 1
    assert answer["api_url"] == "http://exporter/api/ci-observe/incidents?days=7"
    assert answer["grafana_url"] == ("https://grafana/d/ci-observe-failures/ci-observe-e28094-why-runs-fail"
                                     "?var-days=7")
    assert answer["about"] == {"population": "recorded CI incidents whose window overlaps"}
    assert answer["not_answerable"] == ""
    assert answer["question"] == "Incidents overlapping the last 7 days"


def test_an_incidents_window_may_end_in_the_past_and_is_checked(config, web):
    end = days_ago(3)
    web.add(f"http://exporter/api/ci-observe/incidents?days=7&end={end}T00%3A00%3A00Z",
            incidents_document([]))
    answer = tool_call(config, "incidents", {"days": 7, "end": end})
    assert answer["question"] == f"Incidents overlapping the 7 days before {end}T00:00:00Z"
    assert answer["not_answerable"] == "no recorded incident overlaps this window"
    for bad in (0, 91):
        with pytest.raises(mcp.ToolError, match="days"):
            tool_call(config, "incidents", {"days": bad})
    assert web.calls == [f"http://exporter/api/ci-observe/incidents?days=7&end={end}T00%3A00%3A00Z"]


def test_an_incidents_file_that_cannot_be_read_is_not_answerable(config, web):
    web.add("http://exporter/api/ci-observe/incidents?days=7",
            incidents_document([], unavailable="the incidents file /app/incidents.yml cannot be read"))
    answer = tool_call(config, "incidents", {"days": 7})
    assert answer["rows"] == []
    assert answer["not_answerable"] == "the incidents file /app/incidents.yml cannot be read"


def test_the_incident_note_reaches_the_failure_answers(config, web):
    """The exporter writes the one-line incident context into the health and
    failure documents; the tools carry the documents' notes, so the answers
    a window-overlap question lands on say what the numbers are."""
    line = ("this window overlaps incident INC-2026-11-install: Install failures from defective snapshots"
            " (identified, confirmed) — Install failures doubled weekly from Nov 5. (see the incidents tool)")
    web.add("http://exporter/api/ci-observe/failures?days=7&suite=vmaas&trigger=pr&repo=osac", {
        "causes": [{"cause": "install"}], "signatures": [{"signature": "cert-manager cannot get lease"}],
        "about": [{}], "window": [{}], "notes": [line],
    })
    web.add("http://exporter/api/ci-observe/health?days=7&suite=vmaas&trigger=pr&repo=osac", {
        "suites": [{"suite": "vmaas", "pass_rate": 0.6}], "about": [{}], "window": [{}], "notes": [line],
    })
    population = {"suite": "vmaas", "trigger": "pr", "repo": "osac", "days": 7}
    answer = tool_call(config, "failure_causes", population)
    assert line in answer["notes"]
    signatures = tool_call(config, "failure_signatures", population)
    assert line in signatures["notes"]
    health = tool_call(config, "suite_health", population)
    assert line in health["notes"]


# -- the HTTP server and the OAuth gate ----------------------------------------------


@pytest.fixture
def server(config, monkeypatch):
    state = mcp.McpState(config)
    mcp.McpHandler.state = state
    httpd = mcp.McpServer(("127.0.0.1", 0), mcp.McpHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


def request(server, method, path, body=None, headers=None):
    import http.client
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    connection.request(method, path, body=body, headers=headers or {})
    answer = connection.getresponse()
    payload = answer.read()
    connection.close()
    return answer, payload


def start_login(server, client_id, redirect_uri="http://localhost:41111/callback",
                challenge=None, state="st"):
    """Run /authorize the way a client does and return the nonce GitHub
    will echo back as the state (the client's parameters stay server-side)."""
    challenge = challenge or mcp._sha256_b64("the-verifier-the-client-made-up")
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    answer, _ = request(server, "GET", f"/authorize?{query}")
    assert answer.status == 302
    location = answer.getheader("Location")
    assert location.startswith("https://github.com/login/oauth/authorize")
    return urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["state"][0]


def test_the_well_known_documents_name_the_endpoints(server):
    answer, payload = request(server, "GET", "/.well-known/oauth-authorization-server")
    document = json.loads(payload)
    assert answer.status == 200
    assert document["issuer"] == "https://outside"
    assert document["authorization_endpoint"] == "https://outside/authorize"
    # RFC 8414 names: a client's schema rejects the document without them.
    assert document["response_types_supported"] == ["code"]
    assert document["grant_types_supported"] == ["authorization_code"]
    assert document["token_endpoint_auth_methods_supported"] == ["none"]
    assert document["code_challenge_methods_supported"] == ["S256"]


def test_registration_accepts_only_loopback_redirects(server):
    answer, payload = request(server, "POST", "/register",
                              body=json.dumps({"redirect_uris": ["http://localhost:41111/callback"]}),
                              headers={"Content-Type": "application/json"})
    assert answer.status == 201
    assert json.loads(payload)["client_id"]
    answer, _ = request(server, "POST", "/register",
                        body=json.dumps({"redirect_uris": ["https://evil.example/callback"]}),
                        headers={"Content-Type": "application/json"})
    assert answer.status == 400


def test_a_401_leaves_the_keep_alive_connection_usable(server, config, monkeypatch):
    """The 401 must not poison the connection: an unread request body
    becomes the next request's request line, and the client's retry then
    arrives as garbage (a 501 quoting the body). One connection, two
    requests: refused, then served."""
    import http.client
    token = mcp.mint_token(config.session_secret, "dev", time.time())
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25"}})
    connection.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json"})
    first = connection.getresponse()
    first.read()
    assert first.status == 401
    connection.request("POST", "/mcp", body=body,
                       headers={"Content-Type": "application/json",
                                "Authorization": f"Bearer {token}"})
    second = connection.getresponse()
    payload = second.read()
    connection.close()
    assert second.status == 200
    assert json.loads(payload)["result"]["protocolVersion"] == "2025-06-18"


def test_an_mcp_call_without_a_token_is_refused_with_the_resource_metadata(server):
    answer, payload = request(server, "POST", "/mcp", body=json.dumps({"jsonrpc": "2.0", "id": 1,
                                                                       "method": "ping"}),
                              headers={"Content-Type": "application/json"})
    assert answer.status == 401
    assert answer.getheader("WWW-Authenticate") == (
        "Bearer resource_metadata=https://outside/.well-known/oauth-protected-resource")


def test_the_full_login_ends_in_a_working_token(server, config, monkeypatch):
    monkeypatch.setattr(mcp, "github_login", lambda config, code: "dev" if code == "gh-code" else "")
    monkeypatch.setattr(mcp, "grafana_admission", lambda config, login: "" if login == "dev" else "no user")

    # 1. Register the client and send the browser to /authorize.
    _, payload = request(server, "POST", "/register",
                         body=json.dumps({"redirect_uris": ["http://localhost:41111/callback"]}),
                         headers={"Content-Type": "application/json"})
    client_id = json.loads(payload)["client_id"]
    verifier = "the-verifier-the-client-made-up"
    nonce = start_login(server, client_id, challenge=mcp._sha256_b64(verifier))

    # 2. GitHub calls back; the login is checked and the client's redirect
    #    receives a code.
    answer, _ = request(server, "GET",
                        f"/oauth/callback?code=gh-code&state={urllib.parse.quote(nonce)}")
    assert answer.status == 302
    back = urllib.parse.urlparse(answer.getheader("Location"))
    assert back.scheme == "http" and back.hostname == "localhost"
    code = urllib.parse.parse_qs(back.query)["code"][0]
    # No browser session: a cookie on this host would be a bearer token
    # every port of the host receives.
    assert answer.getheader("Set-Cookie") is None

    # 3. The client exchanges the code with its verifier (RFC 6749 4.1.3:
    # the exchange carries the client's own id and redirect URI).
    answer, payload = request(server, "POST", "/token",
                              body=urllib.parse.urlencode({"grant_type": "authorization_code",
                                                           "code": code,
                                                           "code_verifier": verifier,
                                                           "client_id": client_id,
                                                           "redirect_uri": "http://localhost:41111/callback"}),
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert answer.status == 200
    token = json.loads(payload)["access_token"]
    assert mcp.read_token(config.session_secret, token, time.time()) == "dev"

    # 4. The token opens the MCP endpoint.
    answer, payload = request(server, "POST", "/mcp",
                              body=json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}),
                              headers={"Content-Type": "application/json",
                                       "Authorization": f"Bearer {token}"})
    assert answer.status == 200
    assert json.loads(payload)["result"]["tools"]


def test_the_state_github_receives_is_a_short_nonce(server, monkeypatch):
    """The client's parameters must not travel through GitHub's state: a
    ~300-character state made GitHub mint codes it then refused at
    exchange (bad_verification_code). GitHub receives a short nonce."""
    _, payload = request(server, "POST", "/register",
                         body=json.dumps({"redirect_uris": ["http://localhost:41111/callback"]}),
                         headers={"Content-Type": "application/json"})
    client_id = json.loads(payload)["client_id"]
    nonce = start_login(server, client_id)
    assert len(nonce) < 64
    assert "http" not in nonce and "%" not in nonce


def test_a_verifier_that_does_not_match_is_refused(server, monkeypatch):
    monkeypatch.setattr(mcp, "github_login", lambda config, code: "dev")
    monkeypatch.setattr(mcp, "grafana_admission", lambda config, login: "")
    _, payload = request(server, "POST", "/register",
                         body=json.dumps({"redirect_uris": ["http://127.0.0.1:41111/callback"]}),
                         headers={"Content-Type": "application/json"})
    client_id = json.loads(payload)["client_id"]
    nonce = start_login(server, client_id, redirect_uri="http://127.0.0.1:41111/callback",
                        challenge=mcp._sha256_b64("right"))
    answer, _ = request(server, "GET",
                        f"/oauth/callback?code=x&state={urllib.parse.quote(nonce)}")
    code = urllib.parse.parse_qs(urllib.parse.urlparse(answer.getheader("Location")).query)["code"][0]
    answer, _ = request(server, "POST", "/token",
                        body=urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
                                                     "code_verifier": "wrong"}),
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert answer.status == 400


def test_a_refused_login_code_names_githubs_reason(config, monkeypatch):
    """The error carries GitHub's own reason, not a generic line: a burned
    code (bad_verification_code) and a wrong secret (incorrect_client_
    credentials) must be tellable apart from the browser."""
    class Refused:
        ok = True
        status_code = 200

        def json(self):
            return {"error": "bad_verification_code",
                    "error_description": "The code passed is incorrect or expired."}

    monkeypatch.setattr(mcp.requests, "post", lambda *args, **kwargs: Refused())
    with pytest.raises(mcp.AuthError, match="bad_verification_code"):
        mcp.github_login(config, "the-code")


def test_the_grafana_check_reads_a_separate_reachable_address(config, web):
    """The VPN-only Grafana name does not resolve on the monitoring host;
    the check goes through MCP_GRAFANA_API_URL (loopback there) while the
    links keep the public name."""
    reachable = mcp.Config(
        bind="127.0.0.1", port=0, exporter_url="http://exporter", grafana_url="https://grafana",
        grafana_api_url="https://127.0.0.1:3000", external_url="https://outside",
        github_client_id="cid", github_client_secret="secret",
        grafana_service_token="token", session_secret="session-secret", github_api_url="https://gh",
        grafana_verify_tls=False,
    )
    web.add("https://127.0.0.1:3000/api/org/users", [grafana_user("dev")])
    assert mcp.grafana_admission(reachable, "dev") == ""
    assert all("127.0.0.1:3000" in call for call in web.calls)


def test_an_unreachable_grafana_is_an_error_not_a_crash(config, web, monkeypatch):
    """A DNS failure in the Grafana check must answer a clear error, not
    kill the request: the browser then reloaded the dead page and the
    consumed nonce read as 'the login state was broken'."""
    monkeypatch.setattr(
        mcp.requests, "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            mcp.requests.exceptions.ConnectionError("Failed to resolve 'osac-ci.redhat.com'")))
    with pytest.raises(mcp.AuthError, match="could not be reached"):
        mcp.grafana_admission(config, "dev")


def test_the_grafana_gate_reads_the_org_user_list(config, web):
    web.add("https://grafana/api/org/users", [grafana_user("dev", role="Admin"), grafana_user("other")])
    assert mcp.grafana_admission(config, "dev") == ""
    assert "no Grafana user" in mcp.grafana_admission(config, "stranger")


def test_a_disabled_grafana_user_is_refused(config, web):
    # Disabling the user is how access is taken away; the gate must follow it.
    web.add("https://grafana/api/org/users", [grafana_user("left-the-team", disabled=True)])
    assert "is disabled" in mcp.grafana_admission(config, "left-the-team")


def test_a_local_grafana_user_does_not_stand_for_a_github_login(config, web):
    # A local account that shares a GitHub account's name is not that account.
    web.add("https://grafana/api/org/users", [grafana_user("ops", labels=[])])
    assert "does not sign in with GitHub" in mcp.grafana_admission(config, "ops")


def test_a_grafana_that_hides_the_user_state_is_an_error(config, web):
    web.add("https://grafana/api/org/users", [{"login": "dev", "authLabels": ["GitHub"]}])
    with pytest.raises(mcp.AuthError, match="isDisabled"):
        mcp.grafana_admission(config, "dev")
    web.add("https://grafana/api/org/users", [{"login": "dev", "isDisabled": False}])
    with pytest.raises(mcp.AuthError, match="authLabels"):
        mcp.grafana_admission(config, "dev")


def test_a_refused_grafana_token_names_the_fix(config, web):
    web.add("https://grafana/api/org/users", {"message": "no"}, status=403)
    with pytest.raises(mcp.AuthError, match="Admin basic role"):
        mcp.grafana_admission(config, "dev")


def test_a_grafana_that_does_not_answer_is_an_error(config, web):
    web.add("https://grafana/api/org/users", {"message": "broken"}, status=502)
    with pytest.raises(mcp.AuthError, match="HTTP 502"):
        mcp.grafana_admission(config, "dev")


def test_a_login_without_a_grafana_user_is_refused(server, monkeypatch):
    monkeypatch.setattr(mcp, "github_login", lambda config, code: "stranger")
    monkeypatch.setattr(mcp, "grafana_admission",
                        lambda config, login: (_ for _ in ()).throw(AssertionError("looked up")))
    answer, payload = request(server, "GET", "/oauth/callback?code=gh&state=broken")
    assert answer.status == 400  # the broken state, before any lookup


def test_a_github_login_without_a_grafana_user_is_refused(server, monkeypatch):
    monkeypatch.setattr(mcp, "github_login", lambda config, code: "stranger")
    monkeypatch.setattr(mcp, "grafana_admission",
                        lambda config, login: f"the GitHub login {login} has no Grafana user on grafana")
    _, payload = request(server, "POST", "/register",
                         body=json.dumps({"redirect_uris": ["http://localhost:1/cb"]}),
                         headers={"Content-Type": "application/json"})
    client_id = json.loads(payload)["client_id"]
    nonce = start_login(server, client_id, redirect_uri="http://localhost:1/cb")
    answer, payload = request(server, "GET",
                              f"/oauth/callback?code=gh&state={urllib.parse.quote(nonce)}")
    assert answer.status == 403
    assert "no Grafana user" in json.loads(payload)["error"]


# -- the redirect, the client and the limits -------------------------------------------


def register(server, *uris):
    answer, payload = request(server, "POST", "/register", body=json.dumps({"redirect_uris": list(uris)}),
                              headers={"Content-Type": "application/json"})
    return answer, json.loads(payload)


def authorize(server, client_id, redirect_uri, verifier="the-verifier-the-client-made-up", headers=None):
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "code_challenge": mcp._sha256_b64(verifier), "code_challenge_method": "S256", "state": "st"})
    return request(server, "GET", f"/authorize?{query}", headers=headers)


@pytest.mark.parametrize("uri", [
    "http://evil.example\\@localhost/cb",      # Python reads host localhost, a browser evil.example
    "http://evil.example%5C@localhost/cb",
    "http://evil.example%5c@127.0.0.1:8080/cb",
    "http://user@localhost/cb",
    "http://localhost#@evil.example/",
    "https://localhost/cb",
    "http://localhost.evil.example/cb",
    "http://localhost:99999/cb",
    "http://local host/cb",
])
def test_a_redirect_that_is_not_plain_loopback_is_refused(uri):
    assert mcp.loopback_redirect(uri) is None


def test_the_backslash_redirect_can_be_neither_registered_nor_asked_for(server, config):
    """The login theft the review reproduced: a signed-in user's code sent to
    http://evil.example\\@localhost/cb, which a browser opens on evil.example."""
    answer, document = register(server, "http://evil.example\\@localhost/cb")
    assert answer.status == 400
    answer, document = register(server, "http://127.0.0.1/cb")
    answer, payload = authorize(server, document["client_id"], "http://evil.example\\@localhost/cb")
    assert answer.status == 400
    assert json.loads(payload)["error"] == "invalid_redirect_uri"


def test_authorize_admits_only_a_registered_redirect_on_any_port(server):
    _, document = register(server, "http://127.0.0.1:41111/callback")
    client_id = document["client_id"]
    assert authorize(server, client_id, "http://127.0.0.1:41111/callback")[0].status == 302
    # RFC 8252 7.3: a loopback client binds an ephemeral port at login time.
    assert authorize(server, client_id, "http://127.0.0.1:50999/callback")[0].status == 302
    assert authorize(server, client_id, "http://127.0.0.1:41111/elsewhere")[0].status == 400
    assert authorize(server, client_id, "http://localhost:41111/callback")[0].status == 400


def test_a_client_id_this_server_did_not_sign_is_unknown(server, config):
    _, document = register(server, "http://localhost/cb")
    prefix, payload, signature = document["client_id"].split(".")
    forged = f"{prefix}.{mcp._b64(json.dumps(['http://evil.example/cb']).encode())}.{signature}"
    for client_id in (forged, "plain-random-id", ""):
        answer, payload = authorize(server, client_id, "http://localhost/cb")
        assert answer.status == 400 and json.loads(payload)["error"] == "invalid_client"


def test_a_registration_survives_a_restart(server, config):
    # The client id carries its registration: a new process admits it.
    _, document = register(server, "http://localhost:41111/callback")
    mcp.McpHandler.state = mcp.McpState(config)
    assert authorize(server, document["client_id"], "http://localhost:41111/callback")[0].status == 302


def test_login_does_not_skip_github_on_a_cookie(server, config, monkeypatch):
    # The old session cookie minted codes without GitHub or Grafana; it is gone.
    _, document = register(server, "http://localhost:41111/callback")
    cookie = f"ci_observe_mcp_session={mcp.mint_token(config.session_secret, 'victim', time.time())}"
    answer, _ = authorize(server, document["client_id"], "http://localhost:41111/callback", headers={"Cookie": cookie})
    assert answer.getheader("Location").startswith("https://github.com/login/oauth/authorize")


def full_login(server, monkeypatch, redirect="http://localhost:41111/callback", verifier="v" * 43):
    monkeypatch.setattr(mcp, "github_login", lambda config, code: "dev")
    monkeypatch.setattr(mcp, "grafana_admission", lambda config, login: "")
    _, document = register(server, redirect)
    answer, _ = authorize(server, document["client_id"], redirect, verifier=verifier)
    nonce = urllib.parse.parse_qs(urllib.parse.urlparse(answer.getheader("Location")).query)["state"][0]
    answer, _ = request(server, "GET", f"/oauth/callback?code=gh&state={urllib.parse.quote(nonce)}")
    code = urllib.parse.parse_qs(urllib.parse.urlparse(answer.getheader("Location")).query)["code"][0]
    return document["client_id"], code


def exchange(server, **form):
    answer, payload = request(server, "POST", "/token", body=urllib.parse.urlencode(form),
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
    return answer, json.loads(payload)


def test_the_code_is_exchanged_only_by_its_client_and_redirect(server, monkeypatch):
    verifier = "v" * 43
    client_id, code = full_login(server, monkeypatch, verifier=verifier)
    answer, document = exchange(server, grant_type="authorization_code", code=code, code_verifier=verifier,
                                client_id="someone-else")
    assert answer.status == 400 and document["error"] == "invalid_grant"
    client_id, code = full_login(server, monkeypatch, verifier=verifier)
    answer, document = exchange(server, grant_type="authorization_code", code=code, code_verifier=verifier,
                                redirect_uri="http://localhost:41111/other")
    assert answer.status == 400
    client_id, code = full_login(server, monkeypatch, verifier=verifier)
    answer, document = exchange(server, grant_type="authorization_code", code=code, code_verifier=verifier,
                                client_id=client_id, redirect_uri="http://localhost:41111/callback")
    assert answer.status == 200 and document["access_token"]


def raw(server, data, wait=3.0):
    """Send bytes on a fresh connection; return what came back before it
    closed or `wait` passed."""
    sock = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=wait)
    sock.sendall(data)
    received = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            received += chunk
    except TimeoutError:
        pass
    finally:
        sock.close()
    return received


def test_an_oversized_body_is_refused_unread(server):
    # The review's case: 4 GB declared without a token held a thread waiting for it.
    started = time.monotonic()
    answer = raw(server, b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Length: 4000000000\r\n\r\n{")
    assert answer.startswith(b"HTTP/1.1 413")
    assert b"Connection: close" in answer
    assert time.monotonic() - started < 2


@pytest.mark.parametrize(("headers", "status"), [
    (b"Content-Length: lots\r\n", b"400"),
    (b"Transfer-Encoding: chunked\r\n", b"411"),
])
def test_a_body_of_unknown_length_is_refused(server, headers, status):
    answer = raw(server, b"POST /register HTTP/1.1\r\nHost: x\r\n" + headers + b"\r\n")
    assert answer.startswith(b"HTTP/1.1 " + status)


def test_a_silent_connection_is_dropped(server, monkeypatch):
    monkeypatch.setattr(mcp.McpHandler, "timeout", 1)
    started = time.monotonic()
    answer = raw(server, b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{", wait=5)
    assert time.monotonic() - started < 4  # closed by the server, not by the client's patience
    assert not answer.startswith(b"HTTP/1.1 200")


def test_connections_past_the_limit_are_closed_at_once(config):
    mcp.McpHandler.state = mcp.McpState(config)
    httpd = mcp.McpServer(("127.0.0.1", 0), mcp.McpHandler, limit=1)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        holder = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=3)
        holder.sendall(b"POST /mcp HTTP/1.1\r\nHost: x\r\n")  # occupies the one slot
        time.sleep(0.3)
        second = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=3)
        second.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        assert second.recv(100) == b""  # closed without an answer
        second.close()
        holder.close()
    finally:
        httpd.shutdown()


def test_registrations_keep_no_state(server):
    for _ in range(50):
        register(server, "http://localhost/cb")
    assert not hasattr(mcp.McpHandler.state, "clients")


def test_logins_in_flight_are_bounded(server, monkeypatch):
    monkeypatch.setattr(mcp, "MAX_LOGINS_IN_FLIGHT", 2)
    monkeypatch.setattr(mcp, "AUTHORIZES", {})
    _, document = register(server, "http://localhost/cb")
    statuses = [authorize(server, document["client_id"], "http://localhost/cb")[0].status for _ in range(3)]
    assert statuses == [302, 302, 503]


def test_codes_in_flight_are_bounded(monkeypatch):
    monkeypatch.setattr(mcp, "MAX_CODES_IN_FLIGHT", 1)
    monkeypatch.setattr(mcp, "CODES", {})
    assert mcp.mint_code("dev", "c", "http://localhost/cb", "client", 1000.0)
    assert mcp.mint_code("dev", "c", "http://localhost/cb", "client", 1000.0) is None
    # An expired code makes room.
    assert mcp.mint_code("dev", "c", "http://localhost/cb", "client", 1000.0 + mcp.CODE_SECONDS + 1)


def test_a_content_length_of_thousands_of_digits_is_refused_not_crashed(server):
    # isdigit() passes it; int() refuses it. The answer must be a status,
    # not a dropped connection.
    answer = raw(server, b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Length: "
                    + b"9" * 5000 + b"\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 400")


def test_non_ascii_tokens_clients_and_verifiers_are_refused_not_crashed(server, config, monkeypatch):
    """A header is latin-1: a non-ASCII bearer token, client id or code
    verifier would make the digest comparisons raise TypeError. Each is
    answered, and the verifier one is a mismatch, not a crash (the code
    is spent either way: PKCE codes are one use)."""
    answer, payload = request(server, "POST", "/mcp", headers={"Authorization": "Bearer dev.1800000000.\xe9"})
    assert answer.status == 401

    _, document = register(server, "http://localhost/cb")
    answer, payload = authorize(server, "c1.W10.\xe9" * 3, "http://localhost/cb")
    assert answer.status == 400 and json.loads(payload)["error"] == "invalid_client"

    monkeypatch.setattr(mcp, "github_login", lambda config, code: "dev")
    monkeypatch.setattr(mcp, "grafana_admission", lambda config, login: "")
    client_id, code = full_login(server, monkeypatch)
    answer, document = exchange(server, grant_type="authorization_code", code=code,
                                code_verifier="v" * 42 + "\xe9", client_id=client_id,
                                redirect_uri="http://localhost:41111/callback")
    assert answer.status == 400 and document["error"] == "invalid_grant"


def test_a_deeply_nested_body_is_refused_not_crashed(server, config):
    answer, payload = request(server, "POST", "/register", body="[" * 500000,
                              headers={"Content-Type": "application/json"})
    assert answer.status == 400 and json.loads(payload)["error"] == "invalid_client_metadata"
    token = mcp.mint_token(config.session_secret, "dev", time.time())
    answer, payload = request(server, "POST", "/mcp", body="[" * 500000,
                              headers={"Content-Type": "application/json",
                                       "Authorization": f"Bearer {token}"})
    assert answer.status == 400 and json.loads(payload)["error"]["code"] == -32700


def test_a_github_network_blip_mid_login_is_an_error_not_a_crash(server, monkeypatch):
    """The reviewer's case: GitHub times out while exchanging the code; the
    nonce is spent, but the answer names GitHub instead of dropping the
    connection."""
    monkeypatch.setattr(mcp, "github_login",
                        lambda config, code: (_ for _ in ()).throw(
                            mcp.AuthError("GitHub could not be reached to exchange the login code: timeout")))
    _, document = register(server, "http://localhost:41111/callback")
    answer, _ = authorize(server, document["client_id"], "http://localhost:41111/callback")
    nonce = urllib.parse.parse_qs(urllib.parse.urlparse(answer.getheader("Location")).query)["state"][0]
    answer, payload = request(server, "GET", f"/oauth/callback?code=gh&state={urllib.parse.quote(nonce)}")
    assert answer.status == 400
    assert "GitHub could not be reached" in json.loads(payload)["error"]


def test_a_redirect_with_a_bare_fragment_delimiter_is_refused():
    # The parser drops an empty fragment; the code would be appended after
    # "#" where the loopback server never sees it.
    assert mcp.loopback_redirect("http://localhost/cb#") is None


def test_the_token_and_client_signature_domains_are_apart(config):
    token = mcp.mint_token(config.session_secret, "client", time.time())
    prefix, payload, signature = token.split(".")
    forged = f"c1.{payload}.{signature}"
    assert mcp.read_client_id(config.session_secret, forged) is None


def test_the_token_exchange_requires_its_client_and_checks_a_redirect_it_is_sent(server, monkeypatch):
    """A public client names itself with its id (RFC 6749 4.1.3, OAuth 2.1
    alike): leaving it out is a refusal. The redirect URI is checked when
    sent and may be left out: OAuth 2.1 (10.2), which MCP's authorization
    builds on, drops it from the token request, and a client that follows
    only 2.1 does not send it."""
    verifier = "v" * 43
    client_id, code = full_login(server, monkeypatch, verifier=verifier)
    answer, document = exchange(server, grant_type="authorization_code", code=code, code_verifier=verifier)
    assert answer.status == 400 and document["error"] == "invalid_grant"
    client_id, code = full_login(server, monkeypatch, verifier=verifier)
    answer, document = exchange(server, grant_type="authorization_code", code=code, code_verifier=verifier,
                                client_id=client_id)
    assert answer.status == 200 and document["access_token"]
    client_id, code = full_login(server, monkeypatch, verifier=verifier)
    answer, document = exchange(server, grant_type="authorization_code", code=code, code_verifier=verifier,
                                client_id=client_id, redirect_uri="http://localhost:41111/elsewhere")
    assert answer.status == 400 and document["error"] == "invalid_grant"


class Answered:
    """A GitHub answer for github_login: a status and a body that json()
    returns, or raises when it is not JSON (requests raises its
    JSONDecodeError, a RequestException and a ValueError at once)."""

    def __init__(self, status, payload=None, not_json=False):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self._not_json = not_json

    def json(self):
        if self._not_json:
            raise mcp.requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
        return self._payload


@pytest.mark.parametrize(("post", "get", "message"), [
    (mcp.requests.exceptions.ConnectionError("reset"), None, "could not be reached to exchange the login code"),
    (Answered(200, not_json=True), None, "token exchange with something other than JSON"),
    (Answered(200, payload=["not", "an", "object"]), None, "JSON that is not an object"),
    (Answered(200, payload={"access_token": "t"}), mcp.requests.exceptions.Timeout("slow"),
     "could not be reached to say who logged in"),
    (Answered(200, payload={"access_token": "t"}), Answered(200, not_json=True),
     "user lookup with something other than JSON"),
])
def test_github_login_answers_every_github_failure_with_an_auth_error(config, monkeypatch, post, get, message):
    def answer(result):
        def call(*args, **kwargs):
            if isinstance(result, BaseException):
                raise result
            return result
        return call
    monkeypatch.setattr(mcp.requests, "post", answer(post))
    monkeypatch.setattr(mcp.requests, "get", answer(get))
    with pytest.raises(mcp.AuthError, match=message):
        mcp.github_login(config, "code")


@pytest.mark.parametrize(("tool", "kind", "key"), [("time_phases", "time", "phases"), ("waits", "waits", "waits")])
def test_the_time_tools_read_their_documents_rows(config, web, tool, kind, key):
    """Where the time goes, by phase and by product wait: the exporter's
    time and waits documents, which no tool served before 2026-11-22."""
    web.add(f"http://exporter/api/ci-observe/{kind}?days=7&suite=vmaas&trigger=merge_queue&repo=osac",
            {key: [{"name": "install", "median_s": 960}], "about": [{"population": "p"}], "notes": []})
    answer = tool_call(config, tool, {"suite": "vmaas", "trigger": "merge_queue", "repo": "osac", "days": 7})
    assert answer["rows"] == [{"name": "install", "median_s": 960}]
    assert "/d/ci-observe-time/" in answer["grafana_url"]


def test_a_grafana_null_authlabels_is_a_local_account_not_a_broken_answer(config, web):
    # Grafana 13 answers null for a user with no provider entry.
    web.add("https://grafana/api/org/users",
            [{"login": "ops", "isDisabled": False, "authLabels": None}])
    assert "does not sign in with GitHub" in mcp.grafana_admission(config, "ops")


def test_the_callback_log_line_leaves_out_the_login_code(caplog, server, monkeypatch):
    with caplog.at_level("INFO", logger="ci_observe_mcp"):
        request(server, "GET", "/oauth/callback?code=secret-github-code&state=nonce")
    assert "secret-github-code" not in caplog.text
