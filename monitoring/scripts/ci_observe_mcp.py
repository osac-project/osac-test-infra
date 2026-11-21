"""ci-observe over MCP: typed questions about the E2E CI, answered with evidence.

An agent asks a question with every population dimension given (suite,
trigger, repository, window), the server asks the workflow exporter's JSON
API (http://127.0.0.1:9103), and the answer carries the evidence rows (at
most 20), the exact API URL, a Grafana URL with the same variables loaded,
the document's "about" row, and -- when the question cannot be answered --
the reason, plainly. Nothing is interpreted: a missing store, a window
beyond the retention or a GitHub refusal is returned as "not answerable"
with the cause, never as an empty success.

The server reads only the exporter API. No database file, no free-form
query, no second poller.

Authentication (anyone on the VPN with a Grafana user):
1. The MCP client starts the OAuth flow (POST /mcp without a token gets
   401 with the resource metadata URL, as the MCP spec requires).
2. /register accepts a public client (loopback redirect URIs only),
   /authorize sends the browser to GitHub, /oauth/callback exchanges the
   code and checks the GitHub login exists in Grafana through a
   service-account token, /token returns the bearer token (PKCE required).
3. The token is an HMAC over login and expiry signed with
   MCP_SESSION_SECRET; it lives 12 hours and holds no scope: every tool
   may read everything the pages read.

Fail-fast configuration: every required variable is checked at startup and
a missing one stops the server with the full list, before it binds.

Run inside the exporter container image (see quadlet/ci-observe-mcp.container):

    MCP_GITHUB_CLIENT_ID=... MCP_GITHUB_CLIENT_SECRET=... \\
    MCP_GRAFANA_URL=https://osac-ci.redhat.com:3000 \\
    MCP_GRAFANA_SERVICE_TOKEN=... MCP_SESSION_SECRET=... \\
    MCP_EXTERNAL_URL=https://osac-ci-1.redhat.com \\
    python3 ci_observe_mcp.py

MCP_EXTERNAL_URL is the ORIGIN this server is published under. The machine's
firewall leaves only :443 open to the VPN, and HAProxy's SNI route for
osac-ci-1.redhat.com on :443 ends at Caddy's OTLP vhost, so the MCP server
shares that vhost (the Caddyfile routes /mcp and the OAuth endpoints to
127.0.0.1:9105 without the fleet's basic auth; the server's own OAuth gate
protects them). The MCP endpoint itself is {MCP_EXTERNAL_URL}/mcp, and the
OAuth discovery documents sit at {MCP_EXTERNAL_URL}/.well-known/... where
the MCP spec's clients look for them. The GitHub OAuth app's callback URL
is {MCP_EXTERNAL_URL}/oauth/callback. The certificate is Caddy's internal
CA: clients must trust it (the root is at
~/.monitoring-server/data/caddy/caddy/pki/authorities/local/root.crt).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from typing import Callable

import requests
import urllib3

# The Grafana check calls the internal-CA listener with verify=False (the
# cert is issued for the canonical hostname, not for 127.0.0.1); silence
# urllib3's per-call warning so the log carries logins, not TLS notices.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("ci_observe_mcp")

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
SERVER_INFO = {"name": "ci-observe", "version": "1.0.0"}
TOKEN_SECONDS = 12 * 3600
CODE_SECONDS = 120
MAX_ROWS = 20
SESSION_COOKIE = "ci_observe_mcp_session"

SUITES = ("vmaas", "caas", "bmaas")
TRIGGERS = ("pr", "merge_queue", "scheduled", "manual")
REPOS = ("osac", "osac-test-infra")
RESULTS = ("success", "failure", "cancelled")
CAPTURE_ID_RULE = "^(vmaas|caas|bmaas)-(?:pr\\d+|main)-\\d+-a\\d+$"

# The window a tool may ask for, by where its answer lives: the suite-run
# archive and the pull requests are kept 90 days, the exporter's job
# history and the GitHub artifacts 30 days.
INSIGHT_MAX_DAYS = 90
RUNS_MAX_DAYS = 30
PRS_MAX_DAYS = 90


# -- configuration ----------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    bind: str
    port: int
    exporter_url: str
    grafana_url: str
    external_url: str          # the origin this server is published under (https://host:3443)
    github_client_id: str
    github_client_secret: str
    grafana_service_token: str
    session_secret: str
    github_api_url: str
    grafana_verify_tls: bool

    REQUIRED = (
        "MCP_GITHUB_CLIENT_ID",
        "MCP_GITHUB_CLIENT_SECRET",
        "MCP_GRAFANA_URL",
        "MCP_GRAFANA_SERVICE_TOKEN",
        "MCP_SESSION_SECRET",
        "MCP_EXTERNAL_URL",
    )

    @classmethod
    def from_env(cls) -> Config:
        missing = [name for name in cls.REQUIRED if not os.getenv(name, "").strip()]
        if missing:
            raise SystemExit(
                "ci_observe_mcp.py refuses to start: "
                + ", ".join(missing)
                + " must be set (see the module docstring of monitoring/scripts/ci_observe_mcp.py)"
            )
        return cls(
            bind=os.getenv("MCP_BIND", "127.0.0.1"),
            port=int(os.getenv("MCP_PORT", "9105")),
            exporter_url=os.getenv("EXPORTER_URL", "http://127.0.0.1:9103").rstrip("/"),
            grafana_url=os.getenv("MCP_GRAFANA_URL").rstrip("/"),
            external_url=os.getenv("MCP_EXTERNAL_URL").rstrip("/"),
            github_client_id=os.getenv("MCP_GITHUB_CLIENT_ID", ""),
            github_client_secret=os.getenv("MCP_GITHUB_CLIENT_SECRET", ""),
            grafana_service_token=os.getenv("MCP_GRAFANA_SERVICE_TOKEN", ""),
            session_secret=os.getenv("MCP_SESSION_SECRET", ""),
            github_api_url=os.getenv("MCP_GITHUB_API_URL", "https://api.github.com"),
            grafana_verify_tls=os.getenv("MCP_GRAFANA_TLS_VERIFY", "false").lower() == "true",
        )


# -- signed values: the browser session and the bearer token ----------------------


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def mint_token(secret: str, login: str, now: float) -> str:
    expires = int(now + TOKEN_SECONDS)
    payload = f"{login}.{expires}"
    return f"{payload}.{_sign(secret, payload)}"


def read_token(secret: str, token: str, now: float) -> str | None:
    """The login a token carries, or None when it is malformed, forged or
    expired. None is also what an absent token is: the caller answers 401
    either way, with the reason in the log."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[2]:
        return None
    login, expires, signature = parts
    if not login or not expires.isdigit():
        return None
    if not hmac.compare_digest(signature, _sign(secret, f"{login}.{expires}")):
        return None
    if int(expires) < now:
        return None
    return login


# -- GitHub and Grafana ------------------------------------------------------------


class AuthError(Exception):
    """A refusal the browser or the client sees, with the reason."""


def github_login(config: Config, code: str) -> str:
    """The GitHub login behind an OAuth code."""
    answer = requests.post(
        "https://github.com/login/oauth/access_token",
        data={
            "client_id": config.github_client_id,
            "client_secret": config.github_client_secret,
            "code": code,
            "redirect_uri": f"{config.external_url}/oauth/callback",
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    if not answer.ok:
        raise AuthError(f"GitHub would not exchange the login code: HTTP {answer.status_code}")
    body = answer.json()
    access_token = body.get("access_token")
    if not access_token:
        detail = body.get("error") or "no error named"
        description = body.get("error_description") or ""
        raise AuthError(f"GitHub refused the login code: {detail}. {description}".strip())
    access_token = requests.get(
        f"{config.github_api_url}/user",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    if not access_token.ok:
        raise AuthError(f"GitHub would not say who logged in: HTTP {access_token.status_code}")
    login = access_token.json().get("login")
    if not login:
        raise AuthError("GitHub named no login")
    return login


def grafana_user_exists(config: Config, login: str) -> bool:
    """Whether Grafana knows this login: the one gate to the data. Reads
    the org's user list (GET /api/org/users), which a service account with
    the Admin basic role may; the global user search needs a permission to
    grant, and /api/users/lookup is gone in Grafana 13 (404 for the server
    admin too), so neither is used."""
    answer = requests.get(
        f"{config.grafana_url}/api/org/users",
        headers={"Authorization": f"Bearer {config.grafana_service_token}"},
        verify=config.grafana_verify_tls,
        timeout=15,
    )
    if answer.status_code == 200:
        users = answer.json()
        if not isinstance(users, list):
            raise AuthError("Grafana answered its org user list with something other than a list;"
                            " check MCP_GRAFANA_URL")
        return any(isinstance(user, dict) and user.get("login") == login for user in users)
    if answer.status_code in (401, 403):
        raise AuthError(
            f"Grafana refused the service account for the org user list: HTTP {answer.status_code}. "
            "Check MCP_GRAFANA_URL and MCP_GRAFANA_SERVICE_TOKEN; the service account needs the "
            "Admin basic role and nothing more."
        )
    raise AuthError(
        f"Grafana would not answer the org user list: HTTP {answer.status_code}; check MCP_GRAFANA_URL"
    )


# -- the exporter API --------------------------------------------------------------


class ExporterError(Exception):
    """The exporter did not answer; the question is not answerable."""


def fetch_json(config: Config, path: str) -> tuple[int, object]:
    try:
        answer = requests.get(f"{config.exporter_url}{path}", timeout=120)
    except requests.RequestException as error:
        raise ExporterError(f"the exporter at {config.exporter_url} did not answer: {error}") from error
    try:
        body = answer.json()
    except ValueError as error:
        raise ExporterError(f"the exporter answered HTTP {answer.status_code} with no JSON") from error
    return answer.status_code, body


# -- the tools ---------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    question: str
    rows: list[dict]
    api_url: str
    grafana_url: str
    about: dict | None
    window: dict | None
    not_answerable: str
    notes: list[str]

    def document(self) -> dict:
        return {
            "question": self.question,
            "rows": self.rows,
            "api_url": self.api_url,
            "grafana_url": self.grafana_url,
            "about": self.about,
            "window": self.window,
            "not_answerable": self.not_answerable,
            "notes": self.notes,
        }


class ToolError(Exception):
    """A bad argument: the agent must be told which one and what it may be."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[Config, dict], Answer]


def _need_str(arguments: dict, name: str, allowed: tuple[str, ...]) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise ToolError(f"{name} is required (one of: {', '.join(allowed)})")
    if value not in allowed:
        raise ToolError(f"{name} must be one of: {', '.join(allowed)}; got {value!r}")
    return value


def _need_int(arguments: dict, name: str, low: int, high: int) -> int:
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{name} is required, an integer between {low} and {high}")
    if not low <= value <= high:
        raise ToolError(f"{name} must be between {low} and {high}; got {value}")
    return value


def _need_end(arguments: dict) -> str:
    """The optional window end, a UTC date or second-precision time."""
    value = arguments.get("end", "")
    if value in ("", None):
        return ""
    if not isinstance(value, str):
        raise ToolError("end must be a UTC date (2026-11-01) or time (2026-11-01T12:00:00Z)")
    return value


def _insight_query(pop: dict) -> str:
    params = {"days": str(pop["days"]), "suite": pop["suite"], "trigger": pop["trigger"],
              "repo": pop["repo"]}
    if pop["end"]:
        params["end"] = pop["end"]
    return urllib.parse.urlencode(params)


def _grafana_vars(pop: dict) -> str:
    params = [f"var-days={pop['days']}", f"var-suite={pop['suite']}",
              f"var-trigger={pop['trigger']}", f"var-repo={pop['repo']}"]
    return "&".join(params)


def _population_question(kind: str, pop: dict) -> str:
    end = f" ending {pop['end']}" if pop["end"] else ""
    return (f"{kind} of {pop['suite']} suite runs started by {pop['trigger']} in "
            f"{pop['repo']}, last {pop['days']} days{end}")


def _insight_tool(name: str, description: str, kind: str,
                  row_source: Callable[[dict], list], page_uid: str, page_slug: str,
                  question: Callable[[dict], str]) -> Tool:
    """One tool over one insight document: the same population rules, its
    own rows and Grafana page."""

    def handler(config: Config, arguments: dict) -> Answer:
        pop = {
            "suite": _need_str(arguments, "suite", SUITES),
            "trigger": _need_str(arguments, "trigger", TRIGGERS),
            "repo": _need_str(arguments, "repo", REPOS),
            "days": _need_int(arguments, "days", 1, INSIGHT_MAX_DAYS),
            "end": _need_end(arguments),
        }
        status, document = _fetch_document(config, kind, pop)
        not_answerable = _refusals(status, document, kind)
        rows: list[dict] = []
        about = None
        window = None
        notes: list[str] = []
        if not not_answerable:
            rows = [row for row in row_source(document) if isinstance(row, dict)][:MAX_ROWS]
            about = (document.get("about") or [None])[0]
            window = (document.get("window") or [None])[0]
            notes = list(document.get("notes") or [])
            if not rows:
                not_answerable = f"the {kind} document holds no {name} rows in this window"
        return Answer(
            question=question(pop),
            rows=rows,
            api_url=f"{config.exporter_url}/api/ci-observe/{kind}?{_insight_query(pop)}",
            grafana_url=f"{config.grafana_url}/d/{page_uid}/{page_slug}?{_grafana_vars(pop)}",
            about=about,
            window=window,
            not_answerable=not_answerable,
            notes=notes,
        )

    return Tool(
        name=name,
        description=description,
        schema={
            "type": "object",
            "properties": {
                "suite": {"type": "string", "enum": list(SUITES),
                          "description": "The suite: vmaas, caas or bmaas. There is no default."},
                "trigger": {"type": "string", "enum": list(TRIGGERS),
                            "description": "What started the runs. There is no default."},
                "repo": {"type": "string", "enum": list(REPOS),
                         "description": "The repository whose CI started the runs. There is no default."},
                "days": {"type": "integer", "minimum": 1, "maximum": INSIGHT_MAX_DAYS,
                         "description": "The window length in days (the archive keeps 90)."},
                "end": {"type": "string",
                        "description": "Optional window end, a UTC date (2026-11-01) or time"
                                       " (2026-11-01T12:00:00Z). Without it the window ends now."},
            },
            "required": ["suite", "trigger", "repo", "days"],
        },
        handler=handler,
    )


def _fetch_document(config: Config, kind: str, pop: dict, extra: dict | None = None) -> tuple[int, dict]:
    params = dict(_insight_query_pairs(pop))
    params.update(extra or {})
    status, body = fetch_json(config, f"/api/ci-observe/{kind}?{urllib.parse.urlencode(params)}")
    if not isinstance(body, dict):
        raise ExporterError(f"the {kind} document is not an object")
    return status, body


def _insight_query_pairs(pop: dict) -> dict:
    params = {"days": str(pop["days"]), "suite": pop["suite"], "trigger": pop["trigger"],
              "repo": pop["repo"]}
    if pop["end"]:
        params["end"] = pop["end"]
    return params


def _insight_query(pop: dict) -> str:
    return urllib.parse.urlencode(_insight_query_pairs(pop))


def _refusals(status: int, document: dict, kind: str) -> str:
    """Why the document cannot be trusted, or '' when it can. The about row
    carries the stores' own missing data; a refusal here is transport."""
    if status != 200:
        return f"the exporter answered HTTP {status} for the {kind} document"
    if not isinstance(document, dict):
        return f"the {kind} document is not an object"
    return ""


def _rows_of(*keys: str) -> Callable[[dict], list]:
    """The first document key that holds a list of rows."""
    def pick(document: dict) -> list:
        for key in keys:
            rows = document.get(key)
            if isinstance(rows, list):
                return rows
        return []
    return pick


TOOLS: list[Tool] = [
    _insight_tool(
        "suite_health",
        "Runs, pass rate and its change, typical and slow time to result, runner wait and the top"
        " failure cause, per suite. Ask this first for 'is the CI healthy'.",
        "health",
        _rows_of("suites"),
        "ci-observe-home", "ci-observe-e28094-overview",
        lambda pop: _population_question("Health", pop),
    ),
    _insight_tool(
        "failing_tests",
        "The tests that fail most in the window, with failure rate, flaky commits and the last error.",
        "failures",
        _rows_of("tests"),
        "ci-observe-failures", "ci-observe-e28094-why-runs-fail",
        lambda pop: _population_question("Failing tests", pop),
    ),
    _insight_tool(
        "flaky_tests",
        "The tests that passed and failed on the same commit in the window.",
        "failures",
        _rows_of("flaky"),
        "ci-observe-failures", "ci-observe-e28094-why-runs-fail",
        lambda pop: _population_question("Flaky tests", pop),
    ),
    _insight_tool(
        "failure_causes",
        "Why runs fail: causes and their counts, per phase of the first failed step.",
        "failures",
        _rows_of("causes"),
        "ci-observe-failures", "ci-observe-e28094-why-runs-fail",
        lambda pop: _population_question("Failure causes", pop),
    ),
    _insight_tool(
        "time_to_result",
        "Time to result (median, p90, mean), the runner wait and runner hours.",
        "time",
        _rows_of("totals"),
        "ci-observe-time", "ci-observe-e28094-where-the-time-goes",
        lambda pop: _population_question("Time to result", pop),
    ),
    _insight_tool(
        "slowest_tests",
        "The tests that take the most time in the window.",
        "tests",
        _rows_of("tests"),
        "ci-observe-home", "ci-observe-e28094-overview",
        lambda pop: _population_question("Slowest tests", pop),
    ),
]


def _test_history(config: Config, arguments: dict) -> Answer:
    test = arguments.get("test")
    if not isinstance(test, str) or not test.strip():
        raise ToolError("test is required: a canonical test key"
                        " (tests.e2e.vmaas.sanity.test_vm::TestVm::test_create) or a unique substring")
    pop = {
        "suite": _need_str(arguments, "suite", SUITES),
        "trigger": _need_str(arguments, "trigger", TRIGGERS),
        "repo": _need_str(arguments, "repo", REPOS),
        "days": _need_int(arguments, "days", 1, INSIGHT_MAX_DAYS),
        "end": _need_end(arguments),
    }
    params = {"days": str(pop["days"]), "suite": pop["suite"], "trigger": pop["trigger"],
              "repo": pop["repo"], "test": test.strip()}
    if pop["end"]:
        params["end"] = pop["end"]
    status, document = _fetch_document(config, "test", pop, extra={"test": test.strip()})
    not_answerable = _refusals(status, document, "test")
    rows = []
    if not not_answerable:
        summary = document.get("summary")
        runs = document.get("runs")
        rows = ([summary] if isinstance(summary, dict) else []) \
            + [run for run in (runs if isinstance(runs, list) else []) if isinstance(run, dict)][:MAX_ROWS]
        if not rows:
            not_answerable = f"no test matches {test!r} in this window"
    return Answer(
        question=f"History of one test over the last {pop['days']} days",
        rows=rows[:MAX_ROWS],
        api_url=f"{config.exporter_url}/api/ci-observe/test?{urllib.parse.urlencode(params)}",
        grafana_url=(f"{config.grafana_url}/d/ci-observe-test/ci-observe-e28094-test-history"
                     f"?var-test={urllib.parse.quote(test.strip())}&{_grafana_vars(pop)}"),
        about=(document.get("about") or [None])[0] if isinstance(document, dict) else None,
        window=(document.get("window") or [None])[0] if isinstance(document, dict) else None,
        not_answerable=not_answerable,
        notes=list(document.get("notes") or []) if isinstance(document, dict) else [],
    )


def _run_failed_tests(config: Config, arguments: dict) -> Answer:
    capture_id = arguments.get("capture_id")
    if not isinstance(capture_id, str) or not capture_id:
        raise ToolError("capture_id is required, e.g. vmaas-pr2527-44613752917-a1")
    if not re.match(CAPTURE_ID_RULE, capture_id):
        raise ToolError(f"capture_id must look like vmaas-main-41310072671-a1; got {capture_id!r}")
    status, body = fetch_json(config, f"/api/ci-observe/failed-tests?capture_id="
                                      f"{urllib.parse.quote(capture_id)}")
    rows = []
    not_answerable = ""
    notes = []
    if status == 200 and isinstance(body, dict):
        rows = [row for row in body.get("failed_tests") or [] if isinstance(row, dict)][:MAX_ROWS]
        if not rows:
            notes = [f"the junit.xml of {capture_id} lists no failed test"]
    elif isinstance(body, dict) and body.get("error"):
        not_answerable = f"{capture_id}: {body['error']}"
    else:
        not_answerable = f"{capture_id}: the exporter answered HTTP {status}"
    return Answer(
        question=f"The failed tests of {capture_id}",
        rows=rows,
        api_url=f"{config.exporter_url}/api/ci-observe/failed-tests?capture_id="
                f"{urllib.parse.quote(capture_id)}",
        grafana_url=(f"{config.grafana_url}/d/ci-observe-runs/ci-observe-e28094-e2e-run"
                     f"?var-capture_id={urllib.parse.quote(capture_id)}"),
        about=None,
        window=None,
        not_answerable=not_answerable,
        notes=notes,
    )


def _list_runs(config: Config, arguments: dict) -> Answer:
    suite = _need_str(arguments, "suite", SUITES)
    result = _need_str(arguments, "result", RESULTS)
    repo = _need_str(arguments, "repo", REPOS)
    days = _need_int(arguments, "days", 1, RUNS_MAX_DAYS)
    params = {"days": str(days), "repo": repo, "suite": suite, "result": result, "limit": str(MAX_ROWS)}
    status, body = fetch_json(config, f"/api/ci-observe/runs?{urllib.parse.urlencode(params)}")
    rows = []
    not_answerable = ""
    notes = []
    if status == 200 and isinstance(body, dict):
        rows = [row for row in body.get("runs") or [] if isinstance(row, dict)][:MAX_ROWS]
        if body.get("truncated"):
            notes.append("the window holds more runs than shown; narrow the question")
        if not rows:
            not_answerable = "no runs match in this window"
    else:
        not_answerable = f"the exporter answered HTTP {status} for the runs document"
    return Answer(
        question=f"{result} runs of the {suite} suite in {repo}, last {days} days",
        rows=rows,
        api_url=f"{config.exporter_url}/api/ci-observe/runs?{urllib.parse.urlencode(params)}",
        grafana_url=(f"{config.grafana_url}/d/ci-observe-runs-home/ci-observe-e28094-runs"
                     f"?var-days={days}&var-repo={repo}&var-suite={suite}&var-result={result}"),
        about=None,
        window=None,
        not_answerable=not_answerable,
        notes=notes,
    )


def _pull_request_stats(config: Config, arguments: dict) -> Answer:
    repo = _need_str(arguments, "repo", REPOS)
    days = _need_int(arguments, "days", 1, PRS_MAX_DAYS)
    params = {"days": str(days), "repo": repo}
    status, body = fetch_json(config, f"/api/ci-observe/prs?{urllib.parse.urlencode(params)}")
    rows = []
    not_answerable = ""
    if status == 200 and isinstance(body, dict):
        kpis = body.get("kpis")
        funnel = body.get("funnel")
        rows = ([kpis] if isinstance(kpis, dict) else []) \
            + [row for row in (funnel if isinstance(funnel, list) else []) if isinstance(row, dict)]
        if not rows:
            not_answerable = "the pull request store holds no rows in this window"
    else:
        not_answerable = f"the exporter answered HTTP {status} for the pull request document"
    return Answer(
        question=f"Pull request numbers of {repo}, last {days} days",
        rows=rows[:MAX_ROWS],
        api_url=f"{config.exporter_url}/api/ci-observe/prs?{urllib.parse.urlencode(params)}",
        grafana_url=(f"{config.grafana_url}/d/ci-observe-pull-requests/ci-observe-e28094-pull-requests"
                     f"?var-days={days}&var-pr_repo={repo}"),
        about=(body.get("about") or [None])[0] if isinstance(body, dict) else None,
        window=None,
        not_answerable=not_answerable,
        notes=list(body.get("notes") or []) if isinstance(body, dict) else [],
    )


def _pull_request_detail(config: Config, arguments: dict) -> Answer:
    number = arguments.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ToolError("number is required, the pull request number as an integer")
    repo = _need_str(arguments, "repo", REPOS)
    params = {"number": str(number), "repo": repo}
    status, body = fetch_json(config, f"/api/ci-observe/prs?{urllib.parse.urlencode(params)}")
    rows = []
    not_answerable = ""
    if status == 200 and isinstance(body, dict):
        pr = body.get("pr")
        stages = body.get("stages")
        attempts = body.get("attempts")
        rows = ([pr] if isinstance(pr, dict) else []) \
            + [row for row in (stages if isinstance(stages, list) else []) if isinstance(row, dict)] \
            + [row for row in (attempts if isinstance(attempts, list) else []) if isinstance(row, dict)]
        if not rows:
            not_answerable = f"pull request {repo}#{number} is not in the store"
    else:
        not_answerable = f"the exporter answered HTTP {status} for the pull request document"
    return Answer(
        question=f"The stages and queue attempts of {repo}#{number}",
        rows=rows[:MAX_ROWS],
        api_url=f"{config.exporter_url}/api/ci-observe/prs?{urllib.parse.urlencode(params)}",
        grafana_url=(f"{config.grafana_url}/d/ci-observe-pull-requests/ci-observe-e28094-pull-requests"
                     f"?var-pr_number={number}&var-pr_repo={repo}"),
        about=None,
        window=None,
        not_answerable=not_answerable,
        notes=list(body.get("notes") or []) if isinstance(body, dict) else [],
    )


TOOLS.extend([
    Tool(
        name="test_history",
        description="One test over a window: failure rate, duration, its recent runs and where its"
                    " time goes.",
        schema={
            "type": "object",
            "properties": {
                "test": {"type": "string",
                         "description": "The canonical test key, or a substring unique in the window."},
                "suite": {"type": "string", "enum": list(SUITES)},
                "trigger": {"type": "string", "enum": list(TRIGGERS)},
                "repo": {"type": "string", "enum": list(REPOS)},
                "days": {"type": "integer", "minimum": 1, "maximum": INSIGHT_MAX_DAYS},
                "end": {"type": "string",
                        "description": "Optional window end, a UTC date or second-precision time."},
            },
            "required": ["test", "suite", "trigger", "repo", "days"],
        },
        handler=_test_history,
    ),
    Tool(
        name="run_failed_tests",
        description="The failed tests of one recorded run, from its own junit.xml. The capture id is"
                    " the run page's key, e.g. vmaas-pr2527-44613752917-a1.",
        schema={
            "type": "object",
            "properties": {"capture_id": {"type": "string",
                                          "pattern": CAPTURE_ID_RULE,
                                          "description": "suite-(prN|main)-<run id>-a<attempt>"}},
            "required": ["capture_id"],
        },
        handler=_run_failed_tests,
    ),
    Tool(
        name="list_runs",
        description="The runs of one suite and result in a window, newest first (the job history"
                    " keeps 30 days).",
        schema={
            "type": "object",
            "properties": {
                "suite": {"type": "string", "enum": list(SUITES)},
                "result": {"type": "string", "enum": list(RESULTS)},
                "repo": {"type": "string", "enum": list(REPOS)},
                "days": {"type": "integer", "minimum": 1, "maximum": RUNS_MAX_DAYS},
            },
            "required": ["suite", "result", "repo", "days"],
        },
        handler=_list_runs,
    ),
    Tool(
        name="pull_request_stats",
        description="The pull request numbers of a window: lead times, review and queue medians,"
                    " ejections, reruns (the store keeps 90 days).",
        schema={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "enum": list(REPOS)},
                "days": {"type": "integer", "minimum": 1, "maximum": PRS_MAX_DAYS},
            },
            "required": ["repo", "days"],
        },
        handler=_pull_request_stats,
    ),
    Tool(
        name="pull_request_detail",
        description="One pull request: its stages, queue attempts and how each ended.",
        schema={
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "The pull request number."},
                "repo": {"type": "string", "enum": list(REPOS)},
            },
            "required": ["number", "repo"],
        },
        handler=_pull_request_detail,
    ),
])

TOOL_BY_NAME = {tool.name: tool for tool in TOOLS}


def call_tool(config: Config, name: str, arguments: dict) -> dict:
    tool = TOOL_BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"unknown tool {name!r}; call tools/list")
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")
    return tool.handler(config, arguments).document()


# -- JSON-RPC ----------------------------------------------------------------------


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def handle_rpc(config: Config, payload: dict, now: float) -> tuple[HTTPStatus, dict | None]:
    """One JSON-RPC message. Notifications answer 202 with no body."""
    method = payload.get("method")
    if not isinstance(method, str):
        raise JsonRpcError(-32600, "the request names no method")
    message_id = payload.get("id")
    is_request = message_id is not None or "id" in payload

    def result(value: dict) -> tuple[HTTPStatus, dict | None]:
        if not is_request:
            return HTTPStatus.ACCEPTED, None
        return HTTPStatus.OK, {"jsonrpc": "2.0", "id": message_id, "result": value}

    if method == "initialize":
        wanted = payload.get("params", {}).get("protocolVersion")
        version = wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return result({"protocolVersion": version, "capabilities": {"tools": {}},
                       "serverInfo": SERVER_INFO})
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": [{"name": tool.name, "description": tool.description,
                                  "inputSchema": tool.schema} for tool in TOOLS]})
    if method == "tools/call":
        params = payload.get("params") or {}
        try:
            answer = call_tool(config, params.get("name"), params.get("arguments") or {})
        except ToolError as error:
            if not is_request:
                return HTTPStatus.ACCEPTED, None
            return HTTPStatus.OK, {
                "jsonrpc": "2.0", "id": message_id,
                "result": {"content": [{"type": "text", "text": str(error)}],
                           "structuredContent": {"not_answerable": str(error), "rows": []},
                           "isError": True},
            }
        except ExporterError as error:
            if not is_request:
                return HTTPStatus.ACCEPTED, None
            return HTTPStatus.OK, {
                "jsonrpc": "2.0", "id": message_id,
                "result": {"content": [{"type": "text", "text": f"not answerable: {error}"}],
                           "structuredContent": {"not_answerable": str(error), "rows": []},
                           "isError": True},
            }
        return result({
            "content": [{"type": "text", "text": json.dumps(answer, indent=2)}],
            "structuredContent": answer,
        })
    if is_request:
        raise JsonRpcError(-32601, f"unknown method {method!r}")
    return HTTPStatus.ACCEPTED, None


# -- the authorization codes (short-lived, in memory) -------------------------------

CODES: dict[str, dict] = {}


def mint_code(login: str, challenge: str, redirect_uri: str, now: float) -> str:
    code = secrets.token_urlsafe(24)
    CODES[code] = {"login": login, "challenge": challenge, "redirect_uri": redirect_uri,
                   "expires": now + CODE_SECONDS}
    return code


# The logins in flight: /authorize sends GitHub a short random state and
# keeps the client's parameters (its id, redirect URI, PKCE challenge and
# state) here instead. Carrying them through GitHub's state -- a ~300
# character blob -- made GitHub mint codes it then refused at exchange
# (bad_verification_code, isolated by a controlled experiment: the same
# authorize with a short state exchanges fine), so the blob never leaves
# this process. Fifteen minutes of life; a restart forces a new login.
AUTHORIZES: dict[str, dict] = {}
AUTHORIZE_SECONDS = 15 * 60


def mint_authorize(redirect_uri: str, challenge: str, state: str, now: float) -> str:
    nonce = secrets.token_urlsafe(16)
    AUTHORIZES[nonce] = {"redirect_uri": redirect_uri, "challenge": challenge,
                         "state": state, "expires": now + AUTHORIZE_SECONDS}
    for key in [key for key, entry in AUTHORIZES.items() if entry["expires"] < now]:
        AUTHORIZES.pop(key, None)
    return nonce


def take_authorize(nonce: str, now: float) -> dict | None:
    entry = AUTHORIZES.pop(nonce, None)
    if entry is None or entry["expires"] < now:
        return None
    return entry


# -- the HTTP server ----------------------------------------------------------------


class McpState:
    """What the handler needs beyond the config: the signed secrets and the
    in-flight authorization codes."""

    def __init__(self, config: Config):
        self.config = config
        self.clients: set[str] = set()


class McpHandler(BaseHTTPRequestHandler):
    state: McpState
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        logger.info("%s %s", self.address_string(), fmt % args)

    # -- helpers ------------------------------------------------------------

    def _send(self, status: HTTPStatus, body: bytes, content_type: str,
              extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, document: object,
                   extra_headers: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(document).encode(), "application/json", extra_headers)

    def _redirect(self, location: str, cookies: list[str] | None = None) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _external(self, path: str) -> str:
        return f"{self.state.config.external_url}{path}"

    # -- OAuth: the discovery documents and the token endpoint -------------

    def _well_known(self) -> None:
        config = self.state.config
        if self.path == "/.well-known/oauth-protected-resource":
            self._send_json(HTTPStatus.OK, {
                "resource": self._external("/mcp"),
                "authorization_servers": [config.external_url],
            })
            return
        self._send_json(HTTPStatus.OK, {
            "issuer": config.external_url,
            "authorization_endpoint": self._external("/authorize"),
            "token_endpoint": self._external("/token"),
            "registration_endpoint": self._external("/register"),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
        })

    def _register(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_client_metadata"})
            return
        redirect_uris = request.get("redirect_uris") or []
        if not isinstance(redirect_uris, list) or not redirect_uris:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_redirect_uri",
                                                     "error_description": "redirect_uris is required"})
            return
        for uri in redirect_uris:
            parsed = urllib.parse.urlparse(uri)
            if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1"):
                self._send_json(HTTPStatus.BAD_REQUEST, {
                    "error": "invalid_redirect_uri",
                    "error_description": "only loopback http redirect URIs are accepted",
                })
                return
        client_id = secrets.token_urlsafe(16)
        self.state.clients.add(client_id)
        self._send_json(HTTPStatus.CREATED, {"client_id": client_id,
                                             "redirect_uris": redirect_uris})

    def _authorize(self, now: float) -> None:
        config = self.state.config
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        response_type = (query.get("response_type") or [""])[0]
        client_id = (query.get("client_id") or [""])[0]
        redirect_uri = (query.get("redirect_uri") or [""])[0]
        challenge = (query.get("code_challenge") or [""])[0]
        challenge_method = (query.get("code_challenge_method") or [""])[0]
        state = (query.get("state") or [""])[0]
        if response_type != "code":
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "unsupported_response_type"})
            return
        if client_id not in self.state.clients:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_client",
                                                     "error_description": "unknown client_id;"
                                                     " register first"})
            return
        parsed = urllib.parse.urlparse(redirect_uri)
        if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1"):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_redirect_uri"})
            return
        if not challenge or challenge_method != "S256":
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request",
                                                     "error_description": "PKCE with S256 is required"})
            return
        cookies = SimpleCookie()
        cookies.load(self.headers.get("Cookie") or "")
        morsel = cookies.get(SESSION_COOKIE)
        login = read_token(config.session_secret, morsel.value, now) if morsel else None
        if login:
            code = mint_code(login, challenge, redirect_uri, now)
            target = f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}code={urllib.parse.quote(code)}"
            if state:
                target += f"&state={urllib.parse.quote(state)}"
            self._redirect(target)
            return
        # The client's parameters stay here (see AUTHORIZES); GitHub gets a
        # short nonce as the state.
        nonce = mint_authorize(redirect_uri, challenge, state, now)
        github = ("https://github.com/login/oauth/authorize?"
                  + urllib.parse.urlencode({
                      "client_id": config.github_client_id,
                      "redirect_uri": self._external("/oauth/callback"),
                      "scope": "read:user",
                      "state": nonce,
                  }))
        self._redirect(github)

    def _oauth_callback(self, now: float) -> None:
        config = self.state.config
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        code = (query.get("code") or [""])[0]
        nonce = (query.get("state") or [""])[0]
        if not code or not nonce:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "the login was refused"})
            return
        entry = take_authorize(nonce, now)
        if entry is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "the login state was broken"})
            return
        redirect_uri = entry["redirect_uri"]
        challenge = entry["challenge"]
        state = entry["state"]
        try:
            login = github_login(config, code)
        except AuthError as error:
            logger.error("the GitHub login of the OAuth callback was refused: %s", error)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        try:
            if not grafana_user_exists(config, login):
                self._send_json(HTTPStatus.FORBIDDEN, {
                    "error": f"the GitHub login {login} has no Grafana user on {config.grafana_url};"
                             " ask for one and log in again",
                })
                return
        except AuthError as error:
            logger.error("the Grafana check of the OAuth callback failed: %s", error)
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        token = mint_token(config.session_secret, login, now)
        # Back to the client's redirect URI with a fresh authorization code.
        auth_code = mint_code(login, challenge, redirect_uri, now)
        target = f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}code={urllib.parse.quote(auth_code)}"
        if state:
            target += f"&state={urllib.parse.quote(state)}"
        self._redirect(target, cookies=[
            f"{SESSION_COOKIE}={token}; Path=/; Max-Age={TOKEN_SECONDS}; HttpOnly; SameSite=Lax"])

    def _token(self, now: float) -> None:
        config = self.state.config
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        if (form.get("grant_type") or [""])[0] != "authorization_code":
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "unsupported_grant_type"})
            return
        code = (form.get("code") or [""])[0]
        verifier = (form.get("code_verifier") or [""])[0]
        entry = CODES.pop(code, None)
        if entry is None or entry["expires"] < now:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_grant",
                                                     "error_description": "the code is unknown or"
                                                     " older than two minutes"})
            return
        if not secrets.compare_digest(_sha256_b64(verifier), entry["challenge"]):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_grant",
                                                     "error_description": "the code verifier"
                                                     " does not match"})
            return
        self._send_json(HTTPStatus.OK, {
            "access_token": mint_token(config.session_secret, entry["login"], now),
            "token_type": "Bearer",
            "expires_in": TOKEN_SECONDS,
        })

    def _mcp(self, now: float) -> None:
        config = self.state.config
        # Read the whole body before any answer, the 401 included: on a
        # keep-alive connection an unread body becomes the next request's
        # request line, and the client's retry arrives as garbage (a 501
        # quoting the body). No body on GET.
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        header = self.headers.get("Authorization") or ""
        login = None
        if header.startswith("Bearer "):
            login = read_token(config.session_secret, header[len("Bearer "):].strip(), now)
        if login is None:
            logger.warning("an MCP call without a valid token was refused")
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "log in first"},
                            {"WWW-Authenticate": f'Bearer resource_metadata='
                                                 f'{self._external("/.well-known/oauth-protected-resource")}'})
            return
        if self.command != "POST":
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "the MCP endpoint answers POST"})
            return
        try:
            payload = json.loads(raw or b"")
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"jsonrpc": "2.0", "id": None,
                                                     "error": {"code": -32700, "message": "no JSON"}})
            return
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"jsonrpc": "2.0", "id": None,
                                                     "error": {"code": -32600,
                                                               "message": "not a JSON-RPC message"}})
            return
        try:
            status, answer = handle_rpc(config, payload, now)
        except JsonRpcError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"jsonrpc": "2.0", "id": payload.get("id"),
                                                     "error": {"code": error.code,
                                                               "message": error.message}})
            return
        if answer is None:
            self._send(status, b"", "application/json")
        else:
            self._send_json(status, answer)

    # -- routing ---------------------------------------------------------------

    def do_GET(self) -> None:
        now = time.time()
        path = urllib.parse.urlparse(self.path).path
        if path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server"):
            self._well_known()
        elif path == "/authorize":
            self._authorize(now)
        elif path == "/oauth/callback":
            self._oauth_callback(now)
        elif path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True})
        elif path == "/mcp":
            self._mcp(now)
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"no {path} here"})

    def do_POST(self) -> None:
        now = time.time()
        path = urllib.parse.urlparse(self.path).path
        if path == "/register":
            self._register()
        elif path == "/token":
            self._token(now)
        elif path == "/mcp":
            self._mcp(now)
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"no {path} here"})

    def do_DELETE(self) -> None:
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "sessions are not stored"})


def _sha256_b64(verifier: str) -> str:
    """The S256 code challenge of a verifier, base64url without padding."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def serve(config: Config) -> None:
    state = McpState(config)
    McpHandler.state = state
    server = ThreadingHTTPServer((config.bind, config.port), McpHandler)
    logger.info("ci-observe MCP server on http://%s:%d (external %s), reading %s",
                config.bind, config.port, config.external_url, config.exporter_url)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The ci-observe MCP server")
    parser.add_argument("--check-config", action="store_true",
                        help="validate the configuration and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_env()
    if args.check_config:
        logger.info("configuration is complete: %s", config.external_url)
        return 0
    serve(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
