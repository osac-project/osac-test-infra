"""The ci-observe MCP server (ci_observe_mcp.py): typed questions over the
exporter API, answered with rows, the exact API URL, a Grafana URL and the
about row -- and a plain reason when the question is not answerable. Plus
the GitHub-OAuth gate that admits anyone with a Grafana user and nobody
else."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import ThreadingHTTPServer
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
        "suite_health", "failing_tests", "flaky_tests", "failure_causes", "time_to_result",
        "slowest_tests", "test_history", "run_failed_tests", "list_runs",
        "pull_request_stats", "pull_request_detail",
    }
    # Every population dimension is required with no default: the agent
    # must ask which suite, trigger, repository and window it means.
    assert tools["suite_health"]["inputSchema"]["required"] == ["suite", "trigger", "repo", "days"]
    assert "enum" in tools["suite_health"]["inputSchema"]["properties"]["suite"]


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


def test_an_end_reaches_the_api_and_the_question(config, web):
    web.add("http://exporter/api/ci-observe/failures?days=7&suite=vmaas&trigger=pr&repo=osac"
            "&end=2026-11-01", {"flaky": [{"test": "t"}], "about": [{}], "window": [{}], "notes": []})
    answer = tool_call(config, "flaky_tests",
                       {"suite": "vmaas", "trigger": "pr", "repo": "osac", "days": 7,
                        "end": "2026-11-01"})
    assert answer["api_url"].endswith("end=2026-11-01")
    assert "ending 2026-11-01" in answer["question"]


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
    web.add("http://exporter/api/ci-observe/prs?days=30&repo=osac",
            {"kpis": {"merged": 10}, "funnel": [{"stage": "review"}], "about": [{}], "notes": []})
    answer = tool_call(config, "pull_request_stats", {"repo": "osac", "days": 30})
    assert answer["rows"] == [{"merged": 10}, {"stage": "review"}]


# -- the HTTP server and the OAuth gate ----------------------------------------------


@pytest.fixture
def server(config, monkeypatch):
    state = mcp.McpState(config)
    mcp.McpHandler.state = state
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mcp.McpHandler)
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
    monkeypatch.setattr(mcp, "grafana_user_exists", lambda config, login: login == "dev")

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
    headers = dict(answer.getheaders())
    assert headers.get("Set-Cookie", "").startswith("ci_observe_mcp_session=")

    # 3. The client exchanges the code with its verifier.
    answer, payload = request(server, "POST", "/token",
                              body=urllib.parse.urlencode({"grant_type": "authorization_code",
                                                           "code": code,
                                                           "code_verifier": verifier}),
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
    monkeypatch.setattr(mcp, "grafana_user_exists", lambda config, login: True)
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
    web.add("https://127.0.0.1:3000/api/org/users", [{"login": "dev"}])
    assert mcp.grafana_user_exists(reachable, "dev") is True
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
        mcp.grafana_user_exists(config, "dev")


def test_the_grafana_gate_reads_the_org_user_list(config, web):
    web.add("https://grafana/api/org/users",
            [{"login": "dev", "role": "Admin"}, {"login": "other", "role": "Viewer"}])
    assert mcp.grafana_user_exists(config, "dev") is True
    assert mcp.grafana_user_exists(config, "stranger") is False


def test_a_refused_grafana_token_names_the_fix(config, web):
    web.add("https://grafana/api/org/users", {"message": "no"}, status=403)
    with pytest.raises(mcp.AuthError, match="Admin basic role"):
        mcp.grafana_user_exists(config, "dev")


def test_a_grafana_that_does_not_answer_is_an_error(config, web):
    web.add("https://grafana/api/org/users", {"message": "broken"}, status=502)
    with pytest.raises(mcp.AuthError, match="HTTP 502"):
        mcp.grafana_user_exists(config, "dev")


def test_a_login_without_a_grafana_user_is_refused(server, monkeypatch):
    monkeypatch.setattr(mcp, "github_login", lambda config, code: "stranger")
    monkeypatch.setattr(mcp, "grafana_user_exists",
                        lambda config, login: (_ for _ in ()).throw(AssertionError("looked up")))
    answer, payload = request(server, "GET", "/oauth/callback?code=gh&state=broken")
    assert answer.status == 400  # the broken state, before any lookup


def test_a_github_login_without_a_grafana_user_is_refused(server, monkeypatch):
    monkeypatch.setattr(mcp, "github_login", lambda config, code: "stranger")
    monkeypatch.setattr(mcp, "grafana_user_exists", lambda config, login: False)
    _, payload = request(server, "POST", "/register",
                         body=json.dumps({"redirect_uris": ["http://localhost:1/cb"]}),
                         headers={"Content-Type": "application/json"})
    client_id = json.loads(payload)["client_id"]
    nonce = start_login(server, client_id, redirect_uri="http://localhost:1/cb")
    answer, payload = request(server, "GET",
                              f"/oauth/callback?code=gh&state={urllib.parse.quote(nonce)}")
    assert answer.status == 403
    assert "no Grafana user" in json.loads(payload)["error"]
