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

Authentication (anyone on the VPN with an enabled, GitHub-signed-in Grafana
user):
1. The MCP client starts the OAuth flow (POST /mcp without a token gets
   401 with the resource metadata URL, as the MCP spec requires).
2. /register accepts a public client with plain loopback redirect URIs
   (http://localhost or http://127.0.0.1, no user part, no backslash) and
   answers a client id that carries those URIs, signed with
   MCP_SESSION_SECRET: nothing is kept in memory, so a restart forgets no
   client. /authorize admits only a redirect URI the client registered
   (any port, as RFC 8252 asks for loopback) and sends the browser to
   GitHub every time -- GitHub's own session makes that one hop for a
   signed-in user. /oauth/callback exchanges the code and admits the
   GitHub login only when Grafana has an enabled user who signs in with
   GitHub under that login. /token returns the bearer token (PKCE S256
   required; the client id and redirect URI, when sent, must be the
   authorization's).
3. The token is an HMAC over login and expiry signed with
   MCP_SESSION_SECRET; it lives 12 hours and holds no scope: every tool
   may read everything the pages read. There is no browser session: a
   cookie on this host would be a bearer token every port of the host
   receives.

Limits: a request body is at most 1 MiB, a connection is dropped after 30
seconds of silence, at most 64 connections are served at once, and the
logins and codes in flight are bounded; past a bound the answer is an
error, never a silent drop.

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
import binascii
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
# The most runs one list_runs page may hold when the caller asks for more
# than MAX_ROWS: a population question over a week (152 failed runs) is a
# few pages, not eight.
RUNS_MAX_PAGE = 100

# What one unauthenticated caller may hold. A body is read before the 401
# (an unread body on a keep-alive connection becomes the next request's
# request line), so it is bounded first; a silent connection is dropped;
# the connections served at once and the logins and codes in flight are
# capped. Past a bound the answer says so.
MAX_BODY_BYTES = 1 << 20
SOCKET_TIMEOUT_SECONDS = 30
MAX_CONNECTIONS = 64
MAX_LOGINS_IN_FLIGHT = 500
MAX_CODES_IN_FLIGHT = 500
MAX_REDIRECT_URIS = 10

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
    grafana_url: str          # the address browsers use for Grafana links
    grafana_api_url: str      # the address this server reads Grafana through; the
                              # VPN-only name does not resolve on the host, so this
                              # is the loopback listener by default of deployment
    external_url: str         # the origin this server is published under (https://host:3443)
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
            grafana_api_url=(os.getenv("MCP_GRAFANA_API_URL") or os.getenv("MCP_GRAFANA_URL", "")).rstrip("/"),
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
    # The "token." prefix keeps the token's signature domain apart from
    # the client id's ("client."): a login literally named "client" would
    # otherwise sign a string read_client_id accepts.
    return f"{payload}.{_sign(secret, 'token.' + payload)}"


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
    # The comparison runs on bytes: a non-ASCII signature (a header is
    # latin-1) would make compare_digest raise TypeError instead of
    # refusing the token.
    if not hmac.compare_digest(signature.encode(), _sign(secret, "token." + f"{login}.{expires}").encode()):
        return None
    if int(expires) < now:
        return None
    return login


# -- the registered clients: signed, so nothing is kept -----------------------------

CLIENT_ID_PREFIX = "c1"


def loopback_redirect(uri: object) -> urllib.parse.SplitResult | None:
    """The parsed URI when it is a plain loopback http redirect, else None.

    Plain means what a browser and Python read alike: http to localhost or
    127.0.0.1, an optional port, no user part, no fragment, no whitespace
    and no backslash in any spelling. Python's parser reads
    http://evil.example\\@localhost/cb as host localhost; a browser goes to
    evil.example with the code."""
    if not isinstance(uri, str) or not uri or len(uri) > 2000:
        return None
    if any(char in uri for char in "\\ \t\r\n") or "%5c" in uri.lower():
        return None
    try:
        parsed = urllib.parse.urlsplit(uri)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1"):
        return None
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return None
    # A bare "#" is an empty fragment the parser drops; the code would be
    # appended after it, where the loopback server never sees it.
    if uri.rstrip().endswith("#"):
        return None
    if parsed.netloc.lower() != parsed.hostname + (f":{port}" if port is not None else ""):
        return None
    return parsed


def same_redirect(registered: str, requested: str) -> bool:
    """Whether a requested redirect is one the client registered. A loopback
    redirect may name any port (RFC 8252 7.3: the client binds an ephemeral
    one at login time); everything else must be the same."""
    mine, theirs = loopback_redirect(registered), loopback_redirect(requested)
    if mine is None or theirs is None:
        return False
    return (mine.scheme, mine.hostname, mine.path or "/", mine.query) == \
        (theirs.scheme, theirs.hostname, theirs.path or "/", theirs.query)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint_client_id(secret: str, redirect_uris: list[str]) -> str:
    """A client id that carries the client's redirect URIs, signed: the
    registration survives restarts and costs no memory."""
    payload = _b64(json.dumps(redirect_uris, separators=(",", ":")).encode())
    return f"{CLIENT_ID_PREFIX}.{payload}.{_sign(secret, 'client.' + payload)}"


def read_client_id(secret: str, client_id: str) -> list[str] | None:
    """The redirect URIs a client id was registered with, or None when it
    is not one this server signed."""
    parts = (client_id or "").split(".")
    if len(parts) != 3 or parts[0] != CLIENT_ID_PREFIX:
        return None
    _, payload, signature = parts
    # Bytes: a non-ASCII signature would make compare_digest raise
    # TypeError instead of refusing the client id.
    if not hmac.compare_digest(signature.encode(), _sign(secret, "client." + payload).encode()):
        return None
    try:
        uris = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(uris, list) or not all(isinstance(uri, str) for uri in uris):
        return None
    return uris


# -- GitHub and Grafana ------------------------------------------------------------


class AuthError(Exception):
    """A refusal the browser or the client sees, with the reason."""


def github_login(config: Config, code: str) -> str:
    """The GitHub login behind an OAuth code."""
    try:
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
    except ValueError:
        # First: requests' JSONDecodeError is a RequestException too, and an
        # answer that is not JSON is not a GitHub that cannot be reached.
        raise AuthError("GitHub answered its token exchange with something other than JSON") from None
    except requests.exceptions.RequestException as error:
        # A network blip mid-login is an error naming GitHub, never a
        # traceback that drops the connection; the login restarts from
        # /authorize either way.
        raise AuthError(f"GitHub could not be reached to exchange the login code: {error}") from error
    if not isinstance(body, dict):
        raise AuthError("GitHub answered its token exchange with JSON that is not an object")
    access_token = body.get("access_token")
    if not access_token:
        detail = body.get("error") or "no error named"
        description = body.get("error_description") or ""
        raise AuthError(f"GitHub refused the login code: {detail}. {description}".strip())
    try:
        who = requests.get(
            f"{config.github_api_url}/user",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if not who.ok:
            raise AuthError(f"GitHub would not say who logged in: HTTP {who.status_code}")
        found = who.json()
    except ValueError:
        raise AuthError("GitHub answered its user lookup with something other than JSON") from None
    except requests.exceptions.RequestException as error:
        raise AuthError(f"GitHub could not be reached to say who logged in: {error}") from error
    login = found.get("login") if isinstance(found, dict) else None
    if not login:
        raise AuthError("GitHub named no login")
    return login


def grafana_admission(config: Config, login: str) -> str:
    """Why Grafana does not admit this GitHub login, or "" when it does: the
    one gate to the data. Admitted is an org user with this login that is
    not disabled and signs in with GitHub -- a disabled user is someone
    whose access was taken away, and a local user who happens to share a
    GitHub account's name is not that account. Reads the org's user list
    (GET /api/org/users), which a service account with the Admin basic role
    may; the global user search needs a permission to grant, and
    /api/users/lookup is gone in Grafana 13 (404 for the server admin too),
    so neither is used. A Grafana that cannot be reached is an error naming
    the address, never a crash."""
    users = _grafana_org_users(config)
    matches = [user for user in users if isinstance(user, dict) and user.get("login") == login]
    if not matches:
        return f"the GitHub login {login} has no Grafana user on {config.grafana_url}; ask for one and log in again"
    user = matches[0]
    if user.get("isDisabled") is not False:
        if user.get("isDisabled") is True:
            return f"the Grafana user {login} on {config.grafana_url} is disabled"
        raise AuthError("Grafana's org user list does not say whether users are disabled (no isDisabled);"
                        " check MCP_GRAFANA_URL points at Grafana 10 or newer")
    labels = user.get("authLabels")
    # Grafana 13 leaves authLabels null for a user with no provider entry:
    # a present null is "signs in some other way" (the 403 below), not a
    # broken answer; a missing key or a non-list still is.
    if labels is None and "authLabels" in user:
        labels = []
    if not isinstance(labels, list):
        raise AuthError("Grafana's org user list does not say how users sign in (no authLabels);"
                        " check MCP_GRAFANA_URL points at Grafana 10 or newer")
    if not any(isinstance(label, str) and label.lower() == "github" for label in labels):
        return (f"the Grafana user {login} on {config.grafana_url} does not sign in with GitHub"
                f" ({', '.join(map(str, labels)) or 'a local password'}), so a GitHub login cannot stand for it")
    return ""


def _grafana_org_users(config: Config) -> list:
    """GET /api/org/users through the service account."""
    try:
        answer = requests.get(
            f"{config.grafana_api_url}/api/org/users",
            headers={"Authorization": f"Bearer {config.grafana_service_token}"},
            verify=config.grafana_verify_tls,
            timeout=15,
        )
    except requests.RequestException as error:
        raise AuthError(
            f"Grafana could not be reached at {config.grafana_api_url}: {error}. "
            "The VPN-only hostname does not resolve on the monitoring host; set "
            "MCP_GRAFANA_API_URL to an address the container can reach, such as "
            "https://127.0.0.1:3000."
        ) from error
    if answer.status_code == 200:
        try:
            users = answer.json()
        except ValueError:
            users = None
        if not isinstance(users, list):
            raise AuthError("Grafana answered its org user list with something other than a list;"
                            " check MCP_GRAFANA_URL")
        return users
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
    # How many rows the source held; rows is at most MAX_ROWS of them.
    rows_total: int = field(default=-1)
    # The arguments that ask for the next page of the same window, when
    # there is one: the same call with these added.
    next_page: dict | None = field(default=None)

    def document(self) -> dict:
        document = {
            "question": self.question,
            "rows": self.rows,
            "rows_total": len(self.rows) if self.rows_total < 0 else self.rows_total,
            "api_url": self.api_url,
            "grafana_url": self.grafana_url,
            "about": self.about,
            "window": self.window,
            "not_answerable": self.not_answerable,
            "notes": self.notes,
        }
        if self.next_page:
            document["next_page"] = self.next_page
        return document


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


END_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ")


def _parse_end(value: str) -> datetime | None:
    """A UTC date or second-precision UTC time: the two spellings the
    exporter API reads. Anything else would be answered there for a window
    ending now, which is not the question that was asked."""
    for pattern in END_FORMATS:
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _need_end(arguments: dict, days: int, keep_days: int, store: str, now: datetime | None = None) -> str:
    """The optional window end as the API spells it ("" for now), refused
    unless it is a past moment whose window [end - days, end] lies inside
    the store's retention: a window the store no longer holds would be
    answered from the part it still holds, or as nothing."""
    value = arguments.get("end", "")
    if value in ("", None):
        return ""
    moment = _parse_end(value.strip()) if isinstance(value, str) else None
    if moment is None:
        raise ToolError("end must be a UTC date (2026-11-01) or time (2026-11-01T12:00:00Z);"
                        f" got {value!r}")
    now = now or datetime.now(timezone.utc)
    if moment > now:
        raise ToolError(f"end {value!r} is in the future; leave it out for a window ending now")
    start = moment - timedelta(days=days)
    if now - start > timedelta(days=keep_days):
        raise ToolError(f"the window {start:%Y-%m-%d %H:%M} to {moment:%Y-%m-%d %H:%M} UTC starts before the"
                        f" {keep_days} days {store} keeps; move end forward or shorten days")
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _capped(rows: list, what: str, order: str) -> tuple[list, list[str]]:
    """At most MAX_ROWS rows, and a note when there were more: a cut list
    must not read as the whole answer."""
    if len(rows) <= MAX_ROWS:
        return rows, []
    return rows[:MAX_ROWS], [f"showing {MAX_ROWS} of {len(rows)} {what} ({order}); narrow the question for the rest"]


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
        }
        pop["end"] = _need_end(arguments, pop["days"], INSIGHT_MAX_DAYS, "the suite-run archive")
        status, document = _fetch_document(config, kind, pop)
        not_answerable = _refusals(status, document, kind)
        rows: list[dict] = []
        total = 0
        about = None
        window = None
        notes: list[str] = []
        if not not_answerable:
            every = [row for row in row_source(document) if isinstance(row, dict)]
            total = len(every)
            rows, cut = _capped(every, f"{name} rows", "in the document's order")
            about = (document.get("about") or [None])[0]
            window = (document.get("window") or [None])[0]
            notes = list(document.get("notes") or []) + cut
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
            rows_total=total,
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
                                       " (2026-11-01T12:00:00Z). Without it the window ends now. The"
                                       " window must lie inside the archive's 90 days."},
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
        "failure_signatures",
        "Why runs fail, from the failed runs' own logs: error signatures (the message with ids and"
        " numbers replaced), how many failed and passing runs show each and what share of the"
        " window's indexed failed and passing runs that is, first and last seen, and example runs."
        " Ranked by the failed share minus the passing share (score): the comparison that"
        " separates a cause from noise every cluster logs, which counts alone cannot when most"
        " runs fail. Each row's logs_query is a LogQL query for the example runs' raw lines over"
        " logs_from to logs_to; the notes say how many of the window's runs were indexed.",
        "failures",
        _rows_of("signatures"),
        "ci-observe-failures", "ci-observe-e28094-why-runs-fail",
        lambda pop: _population_question("Failure signatures", pop),
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
    _insight_tool(
        "time_phases",
        "Where a run's time goes, phase by phase (queue, setup, cluster, build, install, tests,"
        " teardown): each phase's median and mean per run and its share of the run, from the"
        " job and step timelines.",
        "time",
        _rows_of("phases"),
        "ci-observe-time", "ci-observe-e28094-where-the-time-goes",
        lambda pop: _population_question("Time per phase", pop),
    ),
    _insight_tool(
        "waits",
        "What the tests waited on the product for, from the in-test spans: each wait's resource"
        " kind and what it waited for, how often, and how long in total and per run.",
        "waits",
        _rows_of("waits"),
        "ci-observe-time", "ci-observe-e28094-where-the-time-goes",
        lambda pop: _population_question("Product waits", pop),
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
    }
    pop["end"] = _need_end(arguments, pop["days"], INSIGHT_MAX_DAYS, "the suite-run archive")
    params = {"days": str(pop["days"]), "suite": pop["suite"], "trigger": pop["trigger"],
              "repo": pop["repo"], "test": test.strip()}
    if pop["end"]:
        params["end"] = pop["end"]
    status, document = _fetch_document(config, "test", pop, extra={"test": test.strip()})
    not_answerable = _refusals(status, document, "test")
    rows = []
    notes = []
    total = 0
    if not not_answerable:
        summary = document.get("summary")
        runs = [run for run in (document.get("runs") if isinstance(document.get("runs"), list) else [])
                if isinstance(run, dict)]
        total = len(runs) + (1 if isinstance(summary, dict) else 0)
        # The summary row first, then the runs: the cut keeps the summary.
        shown, notes = _capped(([summary] if isinstance(summary, dict) else []) + runs, "rows (the summary and"
                               " the test's runs)", "the summary, then the runs newest first")
        rows = shown
        if not rows:
            not_answerable = f"no test matches {test!r} in this window"
    return Answer(
        question=f"History of one test over the last {pop['days']} days",
        rows=rows,
        api_url=f"{config.exporter_url}/api/ci-observe/test?{urllib.parse.urlencode(params)}",
        grafana_url=(f"{config.grafana_url}/d/ci-observe-test/ci-observe-e28094-test-history"
                     f"?var-test={urllib.parse.quote(test.strip())}&{_grafana_vars(pop)}"),
        about=(document.get("about") or [None])[0] if isinstance(document, dict) else None,
        window=(document.get("window") or [None])[0] if isinstance(document, dict) else None,
        not_answerable=not_answerable,
        notes=(list(document.get("notes") or []) if isinstance(document, dict) else []) + notes,
        rows_total=total,
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
    total = 0
    if status == 200 and isinstance(body, dict):
        failed = [row for row in body.get("failed_tests") or [] if isinstance(row, dict)]
        # A run that failed before its tests answers with its indexed
        # error lines: "what failed in this run" has an answer even when
        # no test ever ran.
        lines = [row for row in body.get("error_lines") or [] if isinstance(row, dict)]
        notes = list(body.get("notes") or [])
        if failed:
            total = len(failed)
            rows, cut = _capped(failed, "failed tests", "in the junit.xml's order")
        else:
            total = len(lines)
            rows, cut = _capped(lines, "indexed error lines", "newest first")
        notes += cut
        if not rows:
            notes.append(f"the junit.xml of {capture_id} lists no failed test")
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
        rows_total=total,
    )


def _list_runs(config: Config, arguments: dict) -> Answer:
    suite = _need_str(arguments, "suite", SUITES)
    result = _need_str(arguments, "result", RESULTS)
    repo = _need_str(arguments, "repo", REPOS)
    days = _need_int(arguments, "days", 1, RUNS_MAX_DAYS)
    end = _need_end(arguments, days, RUNS_MAX_DAYS, "the job history")
    limit = _need_int(arguments, "limit", 1, RUNS_MAX_PAGE) if "limit" in arguments else MAX_ROWS
    before = arguments.get("before")
    if before is not None and (not isinstance(before, str) or not before.isascii() or not before.isdigit()
                               or len(before) > 20):
        raise ToolError("before must be the next_page.before of the previous answer: a run id, digits only")
    params = {"days": str(days), "repo": repo, "suite": suite, "result": result, "limit": str(limit)}
    if before:
        # Keyset paging in the exporter: the runs older than this id, in
        # the same window. A page neither repeats nor skips a run.
        params["before"] = before
    question = f"{result} runs of the {suite} suite in {repo}, last {days} days"
    if end:
        # A window ending in the past: the runs of the week a regression
        # started, without paging past everything newer.
        params["end"] = end
        question = f"{result} runs of the {suite} suite in {repo}, the {days} days before {end}"
    status, body = fetch_json(config, f"/api/ci-observe/runs?{urllib.parse.urlencode(params)}")
    rows = []
    not_answerable = ""
    notes = []
    total = 0
    next_page = None
    if status == 200 and isinstance(body, dict):
        rows = [row for row in body.get("runs") or [] if isinstance(row, dict)][:limit]
        total = body.get("total") if isinstance(body.get("total"), int) else len(rows)
        cursor = body.get("next_before")
        if cursor and rows:
            next_page = {"before": str(cursor)}
            notes.append(f"this page holds {len(rows)} of the {total} runs in this window, newest first;"
                         f" the same call with before={cursor} returns the next page")
        if not rows:
            not_answerable = "no runs match in this window"
    else:
        not_answerable = f"the exporter answered HTTP {status} for the runs document"
    return Answer(
        question=question,
        rows=rows,
        api_url=f"{config.exporter_url}/api/ci-observe/runs?{urllib.parse.urlencode(params)}",
        grafana_url=(f"{config.grafana_url}/d/ci-observe-runs-home/ci-observe-e28094-runs"
                     f"?var-days={days}&var-repo={repo}&var-suite={suite}&var-result={result}"),
        about=None,
        window=None,
        not_answerable=not_answerable,
        notes=notes,
        rows_total=total,
        next_page=next_page,
    )


def _dict_rows(value: object) -> list[dict]:
    """A document field that holds one row or a list of rows, as rows."""
    if isinstance(value, dict):
        return [value]
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _incidents(config: Config, arguments: dict) -> Answer:
    """The recorded incidents whose window overlaps the asked window. The
    records are git-versioned, not telemetry: the answer distinguishes a
    window no incident overlaps from a file that could not be read."""
    days = _need_int(arguments, "days", 1, INSIGHT_MAX_DAYS)
    end = _need_end(arguments, days, INSIGHT_MAX_DAYS, "the suite-run archive")
    params = {"days": str(days)}
    question = f"Incidents overlapping the last {days} days"
    if end:
        params["end"] = end
        question = f"Incidents overlapping the {days} days before {end}"
    status, body = fetch_json(config, f"/api/ci-observe/incidents?{urllib.parse.urlencode(params)}")
    rows: list[dict] = []
    not_answerable = ""
    notes: list[str] = []
    total = 0
    if status == 200 and isinstance(body, dict):
        every = [row for row in body.get("incidents") or [] if isinstance(row, dict)]
        total = len(every)
        rows, cut = _capped(every, "incidents", "oldest window first")
        notes = list(body.get("notes") or []) + cut
        unavailable = body.get("unavailable")
        if isinstance(unavailable, str) and unavailable:
            not_answerable = unavailable
        elif not rows:
            not_answerable = "no recorded incident overlaps this window"
    else:
        not_answerable = f"the exporter answered HTTP {status} for the incidents document"
    return Answer(
        question=question,
        rows=rows,
        api_url=f"{config.exporter_url}/api/ci-observe/incidents?{urllib.parse.urlencode(params)}",
        # No page of its own: the link opens Why runs fail over the same
        # window, the page whose numbers the incidents explain.
        grafana_url=(f"{config.grafana_url}/d/ci-observe-failures/ci-observe-e28094-why-runs-fail"
                     f"?var-days={days}"),
        about=(body.get("about") or [None])[0] if isinstance(body, dict) else None,
        window=(body.get("window") or [None])[0] if isinstance(body, dict) else None,
        not_answerable=not_answerable,
        notes=notes,
        rows_total=total,
    )


def _pull_request_stats(config: Config, arguments: dict) -> Answer:
    repo = _need_str(arguments, "repo", REPOS)
    days = _need_int(arguments, "days", 1, PRS_MAX_DAYS)
    params = {"days": str(days), "repo": repo}
    status, body = fetch_json(config, f"/api/ci-observe/prs?{urllib.parse.urlencode(params)}")
    rows = []
    not_answerable = ""
    notes = []
    total = 0
    if status == 200 and isinstance(body, dict):
        # The document's kpis is its one row of headline numbers, a list of
        # one; the funnel follows it.
        every = _dict_rows(body.get("kpis")) + _dict_rows(body.get("funnel"))
        total = len(every)
        rows, notes = _capped(every, "rows (the headline numbers, then the funnel)", "in the document's order")
        if not rows:
            not_answerable = "the pull request store holds no rows in this window"
    else:
        not_answerable = f"the exporter answered HTTP {status} for the pull request document"
    return Answer(
        question=f"Pull request numbers of {repo}, last {days} days",
        rows=rows,
        api_url=f"{config.exporter_url}/api/ci-observe/prs?{urllib.parse.urlencode(params)}",
        grafana_url=(f"{config.grafana_url}/d/ci-observe-pull-requests/ci-observe-e28094-pull-requests"
                     f"?var-days={days}&var-pr_repo={repo}"),
        about=(body.get("about") or [None])[0] if isinstance(body, dict) else None,
        window=None,
        not_answerable=not_answerable,
        notes=(list(body.get("notes") or []) if isinstance(body, dict) else []) + notes,
        rows_total=total,
    )


def _pull_request_key(row: dict) -> tuple[str, int]:
    """(moment, number): the pull_requests page order."""
    number = row.get("number")
    return _pull_request_moment(row), number if isinstance(number, int) and not isinstance(number, bool) else 0


def _pull_request_moment(row: dict) -> str:
    """The time the pull request index orders and windows a row by: when it
    merged, else when it closed, else when it opened."""
    return row.get("merged_at") or row.get("closed_at") or row.get("opened_at") or ""


def _pull_requests(config: Config, arguments: dict, now: datetime | None = None) -> Answer:
    """The window's pull requests as rows: number, title, author, when it
    merged, its queue attempts and ejections. This is the "what merged
    around the drop" tool -- the change list behind a pass-rate move."""
    repo = _need_str(arguments, "repo", REPOS)
    days = _need_int(arguments, "days", 1, PRS_MAX_DAYS)
    state = arguments.get("state")
    if not isinstance(state, str) or state not in ("all", "merged", "open", "closed"):
        raise ToolError("state is required: all, merged, open or closed")
    now = now or datetime.now(timezone.utc)
    end = _need_end(arguments, days, PRS_MAX_DAYS, "the pull request store", now)
    before = arguments.get("before")
    cursor = None
    if before is not None:
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\|(\d{1,9})", before) \
            if isinstance(before, str) else None
        if match is None:
            raise ToolError("before must be the next_page.before of the previous answer"
                            " (a moment and a number: 2026-11-06T12:00:00Z|2311)")
        cursor = (match.group(1), int(match.group(2)))
    window_to = _parse_end(end) if end else now
    window_from = window_to - timedelta(days=days)
    # The index windows by the same moment from now; a window ending in the
    # past is fetched back to its start and cut to it here.
    fetch_days = max(1, math.ceil((now - window_from).total_seconds() / 86400))
    params = {"days": str(fetch_days), "repo": repo}
    if end:
        question = f"{state} pull requests of {repo}, the {days} days before {end}"
    else:
        question = f"{state} pull requests of {repo}, last {days} days"
    notes = []
    next_page = None
    status, body = fetch_json(config, f"/api/ci-observe/prs?{urllib.parse.urlencode(params)}")
    rows = []
    total = 0
    not_answerable = ""
    if status == 200 and isinstance(body, dict):
        low, high = window_from.strftime("%Y-%m-%dT%H:%M:%SZ"), window_to.strftime("%Y-%m-%dT%H:%M:%SZ")
        every = [row for row in _dict_rows(body.get("pull_requests") or body.get("prs"))
                 if low <= _pull_request_moment(row) <= high]
        if state != "all":
            every = [row for row in every if (row.get("state") or "") == state]
        # Latest first, ties by number: the order a cursor pages through, so
        # a page neither repeats nor skips a pull request that shares its
        # moment with another, and the window stays the one asked for.
        every.sort(key=_pull_request_key, reverse=True)
        total = len(every)
        page = [row for row in every if cursor is None or _pull_request_key(row) < cursor]
        rows = page[:MAX_ROWS]
        if len(page) > MAX_ROWS:
            moment, number = _pull_request_key(rows[-1])
            next_page = {"before": f"{moment}|{number}"}
            notes.append(f"this page holds {len(rows)} of the {total} pull requests in the window, latest"
                         f" first; the same call with before={next_page['before']} returns the next page")
        if not rows:
            not_answerable = f"the pull request store holds no {state} pull requests of {repo} in this window"
    else:
        not_answerable = f"the exporter answered HTTP {status} for the pull request document"
    keep = ("number", "repo", "title", "author", "state", "opened_at", "merged_at", "closed_at",
            "queue_attempt_count", "ejected_attempt_count", "requeued_attempt_count", "url", "detail_url")
    rows = [{key: row.get(key) for key in keep if row.get(key) is not None} for row in rows]
    return Answer(
        question=question,
        rows=rows,
        api_url=f"{config.exporter_url}/api/ci-observe/prs?{urllib.parse.urlencode(params)}",
        grafana_url=(f"{config.grafana_url}/d/ci-observe-pull-requests/ci-observe-e28094-pull-requests"
                     f"?var-days={days}&var-pr_repo={repo}"),
        about=(body.get("about") or [None])[0] if isinstance(body, dict) else None,
        window={"window_from": window_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "window_to": window_to.strftime("%Y-%m-%dT%H:%M:%SZ")},
        not_answerable=not_answerable,
        notes=notes + (list(body.get("notes") or []) if isinstance(body, dict) else []),
        rows_total=total,
        next_page=next_page,
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
    notes = []
    total = 0
    if status == 200 and isinstance(body, dict):
        every = _dict_rows(body.get("pr")) + _dict_rows(body.get("stages")) + _dict_rows(body.get("attempts"))
        total = len(every)
        rows, notes = _capped(every, "rows (the pull request, its stages, then its queue attempts)",
                              "in that order")
        if not rows:
            not_answerable = f"pull request {repo}#{number} is not in the store"
    else:
        not_answerable = f"the exporter answered HTTP {status} for the pull request document"
    return Answer(
        question=f"The stages and queue attempts of {repo}#{number}",
        rows=rows,
        api_url=f"{config.exporter_url}/api/ci-observe/prs?{urllib.parse.urlencode(params)}",
        grafana_url=(f"{config.grafana_url}/d/ci-observe-pull-requests/ci-observe-e28094-pull-requests"
                     f"?var-pr_number={number}&var-pr_repo={repo}"),
        about=None,
        window=None,
        not_answerable=not_answerable,
        notes=(list(body.get("notes") or []) if isinstance(body, dict) else []) + notes,
        rows_total=total,
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
                        "description": "Optional window end, a UTC date (2026-11-01) or time"
                                       " (2026-11-01T12:00:00Z), inside the archive's 90 days."},
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
                    " keeps 30 days). end moves the window into the past (a UTC date like"
                    " 2026-11-07 or time like 2026-11-07T12:00:00Z), so the runs of the week"
                    " something broke are one call instead of pages past everything newer; the"
                    " window must lie inside the 30 days. A window with more runs than a page"
                    " (limit, 20 by default, at most 100) answers with next_page: the same call"
                    " with its before returns the next page of the same window.",
        schema={
            "type": "object",
            "properties": {
                "suite": {"type": "string", "enum": list(SUITES)},
                "result": {"type": "string", "enum": list(RESULTS)},
                "repo": {"type": "string", "enum": list(REPOS)},
                "days": {"type": "integer", "minimum": 1, "maximum": RUNS_MAX_DAYS},
                "end": {"type": "string",
                        "description": "window end: a UTC date (2026-11-07) or time (2026-11-07T12:00:00Z)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": RUNS_MAX_PAGE,
                          "description": f"runs per page (default {MAX_ROWS})"},
                "before": {"type": "string",
                           "description": "the next page: next_page.before of the previous answer"},
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
        name="pull_requests",
        description="The pull requests of a window as rows, latest first: number, title, author, when"
                    " it merged, queue attempts and ejections. The change list behind a pass-rate"
                    " move: ask 'which PRs merged in this window' here, then pull_request_detail on"
                    " the suspects. end moves the window into the past (a UTC date like 2026-11-06"
                    " or time like 2026-11-06T12:00:00Z), so the merges of the week a regression"
                    " started are one call. More than 20 answer with next_page: the same call with"
                    " its before returns the next page of the same window.",
        schema={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "enum": list(REPOS)},
                "days": {"type": "integer", "minimum": 1, "maximum": PRS_MAX_DAYS},
                "state": {"type": "string", "enum": ["all", "merged", "open", "closed"]},
                "end": {"type": "string",
                        "description": "window end: a UTC date (2026-11-06) or time (2026-11-06T12:00:00Z)"},
                "before": {"type": "string",
                           "description": "the next page: next_page.before of the previous answer"},
            },
            "required": ["repo", "days", "state"],
        },
        handler=_pull_requests,
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
    Tool(
        name="incidents",
        description="The recorded incidents whose window overlaps the asked window: what broke,"
                    " the root-cause chain, evidence links and follow-ups, from the git-versioned"
                    " incidents file a person writes after a post-mortem. Ask this when a window"
                    " looks bad, or when another answer notes it overlaps an incident. The records"
                    " are not telemetry: a window with no incident answers 'no recorded incident"
                    " overlaps this window', never 'nothing broke'. The Grafana link opens Why runs"
                    " fail with the same window, the page whose numbers an incident explains.",
        schema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": INSIGHT_MAX_DAYS,
                         "description": "The window length in days (the archive keeps 90)."},
                "end": {"type": "string",
                        "description": "Optional window end, a UTC date (2026-11-01) or time"
                                       " (2026-11-01T12:00:00Z). Without it the window ends now."},
            },
            "required": ["days"],
        },
        handler=_incidents,
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

    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    if method == "initialize":
        wanted = params.get("protocolVersion")
        version = wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return result({"protocolVersion": version, "capabilities": {"tools": {}},
                       "serverInfo": SERVER_INFO})
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": [{"name": tool.name, "description": tool.description,
                                  "inputSchema": tool.schema} for tool in TOOLS]})
    if method == "tools/call":
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
        except Exception as error:  # a defect here must be an answer, not a reset connection
            logger.exception("the tool %r failed", params.get("name"))
            if not is_request:
                return HTTPStatus.ACCEPTED, None
            return HTTPStatus.OK, {
                "jsonrpc": "2.0", "id": message_id,
                "result": {"content": [{"type": "text", "text": f"the server failed answering: {error!r}"}],
                           "structuredContent": {"not_answerable": f"the server failed answering: {error!r}",
                                                 "rows": []},
                           "isError": True},
            }
        return result({
            "content": [{"type": "text", "text": json.dumps(answer, indent=2)}],
            "structuredContent": answer,
        })
    if is_request:
        raise JsonRpcError(-32601, f"unknown method {method!r}")
    return HTTPStatus.ACCEPTED, None


# -- the logins and codes in flight (short-lived, in memory, bounded) ---------------

# Both maps are shared by the server's threads, so every read and write
# holds the lock; each is purged of expired entries when it grows, and a
# full map refuses a new entry instead of growing without bound.
_IN_FLIGHT_LOCK = threading.Lock()
CODES: dict[str, dict] = {}


def _purge(entries: dict[str, dict], now: float) -> None:
    for key in [key for key, entry in entries.items() if entry["expires"] < now]:
        entries.pop(key, None)


def mint_code(login: str, challenge: str, redirect_uri: str, client_id: str, now: float) -> str | None:
    """A one-use authorization code, or None when MAX_CODES_IN_FLIGHT are
    already waiting to be exchanged."""
    with _IN_FLIGHT_LOCK:
        _purge(CODES, now)
        if len(CODES) >= MAX_CODES_IN_FLIGHT:
            return None
        code = secrets.token_urlsafe(24)
        CODES[code] = {"login": login, "challenge": challenge, "redirect_uri": redirect_uri,
                       "client_id": client_id, "expires": now + CODE_SECONDS}
        return code


def take_code(code: str, now: float) -> dict | None:
    with _IN_FLIGHT_LOCK:
        entry = CODES.pop(code, None)
    if entry is None or entry["expires"] < now:
        return None
    return entry


# The logins in flight: /authorize sends GitHub a short random state and
# keeps the client's parameters (its id, redirect URI, PKCE challenge and
# state) here instead. Carrying them through GitHub's state -- a ~300
# character blob -- made GitHub mint codes it then refused at exchange
# (bad_verification_code, isolated by a controlled experiment: the same
# authorize with a short state exchanges fine), so the blob never leaves
# this process. Fifteen minutes of life; a restart forces a new login.
AUTHORIZES: dict[str, dict] = {}
AUTHORIZE_SECONDS = 15 * 60


def mint_authorize(redirect_uri: str, challenge: str, state: str, client_id: str, now: float) -> str | None:
    """The nonce GitHub carries as the state, or None when
    MAX_LOGINS_IN_FLIGHT logins are already waiting for GitHub."""
    with _IN_FLIGHT_LOCK:
        _purge(AUTHORIZES, now)
        if len(AUTHORIZES) >= MAX_LOGINS_IN_FLIGHT:
            return None
        nonce = secrets.token_urlsafe(16)
        AUTHORIZES[nonce] = {"redirect_uri": redirect_uri, "challenge": challenge, "state": state,
                             "client_id": client_id, "expires": now + AUTHORIZE_SECONDS}
        return nonce


def take_authorize(nonce: str, now: float) -> dict | None:
    with _IN_FLIGHT_LOCK:
        entry = AUTHORIZES.pop(nonce, None)
    if entry is None or entry["expires"] < now:
        return None
    return entry


# -- the HTTP server ----------------------------------------------------------------


class McpState:
    """What the handler needs beyond the config. Nothing about clients is
    kept: a client id carries its registration (see mint_client_id)."""

    def __init__(self, config: Config):
        self.config = config


class BodyRefused(Exception):
    """The request body was refused and answered; the connection closes."""


class McpHandler(BaseHTTPRequestHandler):
    state: McpState
    protocol_version = "HTTP/1.1"
    # A connection silent this long is dropped (StreamRequestHandler puts it
    # on the socket), so a slow or stalled client cannot hold a thread.
    timeout = SOCKET_TIMEOUT_SECONDS

    def log_message(self, fmt: str, *args) -> None:
        # The request line of /oauth/callback carries GitHub's login code:
        # log the path only.
        message = fmt % args
        if "/oauth/callback?" in message:
            message = re.sub(r"/oauth/callback\?\S*", "/oauth/callback?…", message)
        logger.info("%s %s", self.address_string(), message)

    # -- helpers ------------------------------------------------------------

    def _send(self, status: HTTPStatus, body: bytes, content_type: str,
              extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, document: object,
                   extra_headers: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(document).encode(), "application/json", extra_headers)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _external(self, path: str) -> str:
        return f"{self.state.config.external_url}{path}"

    def _body(self) -> bytes:
        """The request body, at most MAX_BODY_BYTES. A body of unknown or
        excessive length is answered (411, 400 or 413) and the connection
        closed, never read: it is how one unauthenticated caller would
        fill the server's memory or hold a thread."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self._refuse(HTTPStatus.LENGTH_REQUIRED, "send a Content-Length, not a chunked body")
        raw = (self.headers.get("Content-Length") or "").strip()
        if not raw:
            return b""
        if not raw.isdigit():
            self._refuse(HTTPStatus.BAD_REQUEST, "Content-Length is not a number")
        try:
            length = int(raw)
        except ValueError:
            # isdigit() passes a 5000-digit number; int() refuses it.
            self._refuse(HTTPStatus.BAD_REQUEST, "Content-Length is not a length")
        if length > MAX_BODY_BYTES:
            self._refuse(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"a request body is at most {MAX_BODY_BYTES} bytes")
        return self.rfile.read(length) if length else b""

    def _refuse(self, status: HTTPStatus, reason: str) -> None:
        self._send_json(status, {"error": reason}, {"Connection": "close"})
        raise BodyRefused(reason)

    # -- OAuth: the discovery documents and the token endpoint -------------

    def _well_known(self) -> None:
        config = self.state.config
        if urllib.parse.urlparse(self.path).path == "/.well-known/oauth-protected-resource":
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
        try:
            request = json.loads(self._body() or b"{}")
        except (ValueError, RecursionError):
            # RecursionError: a body of nested brackets is valid JSON shape
            # but unbounded depth.
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_client_metadata"})
            return
        redirect_uris = request.get("redirect_uris") if isinstance(request, dict) else None
        if not isinstance(redirect_uris, list) or not redirect_uris:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_redirect_uri",
                                                     "error_description": "redirect_uris is required"})
            return
        if len(redirect_uris) > MAX_REDIRECT_URIS or not all(loopback_redirect(uri) for uri in redirect_uris):
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_redirect_uri",
                "error_description": "only plain loopback http redirect URIs are accepted (http://localhost or"
                                     f" http://127.0.0.1, no user part or backslash), at most {MAX_REDIRECT_URIS}",
            })
            return
        client_id = mint_client_id(self.state.config.session_secret, redirect_uris)
        self._send_json(HTTPStatus.CREATED, {"client_id": client_id, "redirect_uris": redirect_uris,
                                             "token_endpoint_auth_method": "none"})

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
        registered = read_client_id(config.session_secret, client_id)
        if registered is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_client",
                                                     "error_description": "unknown client_id; register first"})
            return
        # Never redirect to an address the client did not register: an
        # authorization code sent elsewhere is a login handed to whoever
        # receives it.
        if not any(same_redirect(uri, redirect_uri) for uri in registered):
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_redirect_uri",
                "error_description": "redirect_uri is not one this client registered"})
            return
        if not challenge or challenge_method != "S256":
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request",
                                                     "error_description": "PKCE with S256 is required"})
            return
        # The client's parameters stay here (see AUTHORIZES); GitHub gets a
        # short nonce as the state. Every login goes through GitHub: its
        # session makes that one hop, and no cookie of ours stands for it.
        nonce = mint_authorize(redirect_uri, challenge, state, client_id, now)
        if nonce is None:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "error": "temporarily_unavailable",
                "error_description": f"{MAX_LOGINS_IN_FLIGHT} logins are already waiting for GitHub;"
                                     " try again in a few minutes"})
            return
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
        try:
            login = github_login(config, code)
        except AuthError as error:
            logger.error("the GitHub login of the OAuth callback was refused: %s", error)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        try:
            refusal = grafana_admission(config, login)
        except AuthError as error:
            logger.error("the Grafana check of the OAuth callback failed: %s", error)
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        if refusal:
            logger.warning("the login of %s was refused: %s", login, refusal)
            self._send_json(HTTPStatus.FORBIDDEN, {"error": refusal})
            return
        # Back to the client's redirect URI with a fresh authorization code.
        auth_code = mint_code(login, entry["challenge"], entry["redirect_uri"], entry["client_id"], now)
        if auth_code is None:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "error": f"{MAX_CODES_IN_FLIGHT} login codes are already waiting to be exchanged;"
                         " try again in a few minutes"})
            return
        redirect_uri = entry["redirect_uri"]
        target = f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}code={urllib.parse.quote(auth_code)}"
        if entry["state"]:
            target += f"&state={urllib.parse.quote(entry['state'])}"
        self._redirect(target)

    def _token(self, now: float) -> None:
        config = self.state.config
        form = urllib.parse.parse_qs(self._body().decode("utf-8", "replace"))
        if (form.get("grant_type") or [""])[0] != "authorization_code":
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "unsupported_grant_type"})
            return
        code = (form.get("code") or [""])[0]
        verifier = (form.get("code_verifier") or [""])[0]
        entry = take_code(code, now)
        if entry is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_grant",
                                                     "error_description": "the code is unknown or"
                                                     " older than two minutes"})
            return
        # Bytes: a non-ASCII challenge (stored from a query string) would
        # make compare_digest raise TypeError instead of refusing.
        if not secrets.compare_digest(_sha256_b64(verifier).encode(), entry["challenge"].encode()):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_grant",
                                                     "error_description": "the code verifier"
                                                     " does not match"})
            return
        # The exchange is the authorization's own client and redirect URI.
        # The client id is required: the client is public (no secret to
        # authenticate it), so its id is the only thing that names it
        # (RFC 6749 4.1.3 and OAuth 2.1 alike). The redirect URI is checked
        # when sent and may be left out: OAuth 2.1 (10.2), which MCP's
        # authorization builds on, drops it from the token request, since
        # PKCE binds the code to its client; a server MUST accept and
        # enforce it when a client sends it.
        sent_client = (form.get("client_id") or [""])[0]
        sent_redirect = (form.get("redirect_uri") or [""])[0]
        if sent_client != entry["client_id"] or (sent_redirect and sent_redirect != entry["redirect_uri"]):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_grant",
                                                     "error_description": "the code was issued to another"
                                                     " client or redirect URI"})
            return
        self._send_json(HTTPStatus.OK, {
            "access_token": mint_token(config.session_secret, entry["login"], now),
            "token_type": "Bearer",
            "expires_in": TOKEN_SECONDS,
        })

    def _mcp(self, now: float) -> None:
        config = self.state.config
        # Read the (bounded) body before any answer, the 401 included: on a
        # keep-alive connection an unread body becomes the next request's
        # request line, and the client's retry arrives as garbage (a 501
        # quoting the body). No body on GET.
        raw = self._body()
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
        except (ValueError, RecursionError):
            # RecursionError: a body of nested brackets is valid JSON shape
            # but unbounded depth.
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

    # The routes that read their own body; every other route's body is read
    # (bounded) and dropped first, so it cannot desynchronize a keep-alive
    # connection.
    READS_BODY = frozenset({"/mcp", "/register", "/token"})

    def _route(self, routes: dict[str, Callable[[float], None]]) -> None:
        path = urllib.parse.urlparse(self.path).path
        handler = routes.get(path)
        try:
            if handler is None or path not in self.READS_BODY:
                self._body()
            if handler is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"no {path} here"})
            else:
                handler(time.time())
        except BodyRefused:
            return

    def _no_sessions(self, now: float) -> None:
        self._body()
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "sessions are not stored"})

    def do_GET(self) -> None:
        self._route({
            "/.well-known/oauth-protected-resource": lambda now: self._well_known(),
            "/.well-known/oauth-authorization-server": lambda now: self._well_known(),
            "/authorize": self._authorize,
            "/oauth/callback": self._oauth_callback,
            "/healthz": lambda now: self._send_json(HTTPStatus.OK, {"ok": True}),
            "/mcp": self._mcp,
        })

    def do_POST(self) -> None:
        self._route({
            "/register": lambda now: self._register(),
            "/token": self._token,
            "/mcp": self._mcp,
        })

    def do_DELETE(self) -> None:
        self._route({"/mcp": self._no_sessions})


class McpServer(ThreadingHTTPServer):
    """A thread per connection, at most MAX_CONNECTIONS at once: past that
    a connection is closed at once and logged, not queued."""

    daemon_threads = True

    def __init__(self, address, handler, limit: int = MAX_CONNECTIONS):
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(limit)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            logger.warning("refused a connection from %s: %d are already open", client_address[0],
                           MAX_CONNECTIONS)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def _sha256_b64(verifier: str) -> str:
    """The S256 code challenge of a verifier, base64url without padding."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def serve(config: Config) -> None:
    state = McpState(config)
    McpHandler.state = state
    server = McpServer((config.bind, config.port), McpHandler)
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
